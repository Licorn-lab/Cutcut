"""Local folder background removal; originals are read only."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageChops

VERSION = "1.9.5"
# Change only when image processing or quality screening changes.
PIPELINE_VERSION = "1.7.0"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
MODEL_LABELS = {"自动选模 · 比较候选": "auto", "装裱刺绣保护 · 双模型": "framed-dual", "通用 · IS-Net": "isnet-general-use", "标准 · U²-Net": "u2net", "轻量试跑 · U²-Netp": "u2netp", "细节试跑 · BiRefNet": "birefnet-general"}
MODELS = {"auto", "u2net", "u2netp", "isnet-general-use", "framed-dual", "birefnet-general"}


class ModelPreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Options:
    model: str = "auto"
    layout: str = "original"
    size: int = 1500
    occupancy: float = 0.85
    white_format: str = "jpg"
    recursive: bool = True
    resume: bool = True
    alpha_matting: bool = False

    def validate(self):
        if self.model not in MODELS or self.layout not in {"square", "original"}:
            raise ValueError("模型或画布设置无效")
        if not 256 <= self.size <= 6000 or not 0.5 <= self.occupancy <= 0.95:
            raise ValueError("尺寸应在 256–6000，主体占比应在 50%–95%")
        if self.white_format not in {"jpg", "png"}:
            raise ValueError("白底格式应为 JPG 或 PNG")

    def signature(self):
        values = asdict(self)
        if self.model == "auto":values["auto_revision"]="suspect-first-v1"
        values.pop("resume")
        values.pop("recursive")
        return hashlib.sha256(json.dumps([PIPELINE_VERSION, values], sort_keys=True).encode()).hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()


def collect_images(source, destination, recursive=True):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if not source.is_dir():
        raise ValueError("请选择一个存在的原图文件夹")
    if destination == source or source.is_relative_to(destination):
        raise ValueError("输出位置不能与原图相同，也不能是原图的上级文件夹")
    found = []
    for folder, dirs, files in os.walk(source, followlinks=False):
        base = Path(folder)
        dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                         and not (base / d).is_symlink()
                         and not (base / d).resolve().is_relative_to(destination)) if recursive else []
        for name in sorted(files):
            path = base / name
            if not name.startswith(".") and not path.is_symlink() and path.suffix.lower() in EXTENSIONS:
                found.append(path)
    return sorted(found, key=lambda p: p.relative_to(source).as_posix())


def load_image(path):
    # Mac/iPhone photos may be HEIC; registering it does not change JPEG/PNG decoding.
    if Path(path).suffix.lower() in {".heic", ".heif"}:
        try:
            from pillow_heif import register_heif_opener
            register_heif_opener()
        except ImportError as exc:
            raise ValueError("HEIC 组件未安装，请重新运行安装程序或先导出为 JPG") from exc
    with Image.open(path) as image:
        # MPO is a JPEG container with a primary photo and additional images.
        # Process its primary frame only; never flatten animations or multipage files.
        if image.format == "MPO":
            image.seek(0)
        elif getattr(image, "n_frames", 1) > 1:
            raise ValueError("多页或动画图片请先拆成单张静态图片")
        image = ImageOps.exif_transpose(image)
        # Preserve appearance under embedded color profiles when possible.
        profile = image.info.get("icc_profile")
        if profile:
            from PIL import ImageCms
            from io import BytesIO
            image = ImageCms.profileToProfile(image, ImageCms.ImageCmsProfile(BytesIO(profile)),
                                              ImageCms.createProfile("sRGB"), outputMode="RGBA")
        return image.convert("RGBA")


def render_white(cutout, options):
    cutout = cutout.convert("RGBA")
    alpha = cutout.getchannel("A")
    # A small tolerance ignores imperceptible specks when locating the subject.
    bbox = alpha.point(lambda x: 255 if x > 8 else 0).getbbox()
    if bbox is None:
        raise ValueError("未识别到物品，请换模型或手工处理")
    warnings = []
    if options.layout == "original":
        canvas = Image.new("RGBA", cutout.size, "white")
        canvas.alpha_composite(cutout)
    else:
        # Preserve faint soft edges close to the detected subject.
        pad = max(3, round(max(cutout.size) * 0.005))
        box = (max(0, bbox[0] - pad), max(0, bbox[1] - pad),
               min(cutout.width, bbox[2] + pad), min(cutout.height, bbox[3] + pad))
        subject = cutout.crop(box)
        factor = options.size * options.occupancy / max(subject.size)
        target = (max(1, round(subject.width * factor)), max(1, round(subject.height * factor)))
        subject = subject.resize(target, Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (options.size, options.size), "white")
        canvas.alpha_composite(subject, ((options.size - target[0]) // 2, (options.size - target[1]) // 2))
    return canvas.convert("RGB"), warnings


def save_image_atomic(image, path, fmt, **kwargs):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    os.close(fd)
    try:
        image.save(tmp, format=fmt, **kwargs)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def make_session(model):
    os.environ.setdefault("OMP_NUM_THREADS", str(min(4, os.cpu_count() or 2)))
    cache = Path.home() / "Library" / "Application Support" / "WhiteBackgroundBatch" / "models"
    os.environ.setdefault("U2NET_HOME", str(cache))
    if model == "auto":
        from auto_model import AutoSession
        return AutoSession(make_session, remove_background)
    from rembg import new_session
    # Explicit local model, no cloud backend. CPU works on both Intel and Apple Silicon.
    if model == "framed-dual":
        return tuple(new_session(name, providers=["CPUExecutionProvider"])
                     for name in ("isnet-general-use", "u2net"))
    return new_session(model, providers=["CPUExecutionProvider"])


def framed_mask(masks):
    """Protect the solid convex outline of one framed object, including its interior."""
    import cv2
    import numpy as np
    combined = np.maximum.reduce([np.asarray(mask, dtype=np.uint8) for mask in masks])
    contours, _ = cv2.findContours((combined >= 128).astype(np.uint8),
                                  cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return Image.new("L", masks[0].size, 0)
    hull = cv2.convexHull(np.concatenate(contours))
    filled = np.zeros_like(combined)
    cv2.fillConvexPoly(filled, hull, 255, lineType=cv2.LINE_AA)
    return Image.fromarray(np.maximum(filled, combined))


def remove_background(image, session, options):
    if options.model == "auto":
        return session.predict(image,options)
    from rembg import remove
    if options.model == "framed-dual":
        masks = [remove(image, session=item, only_mask=True).convert("L") for item in session]
        alpha = framed_mask(masks)
    else:
        inferred = remove(image, session=session, alpha_matting=options.alpha_matting).convert("RGBA")
        alpha = inferred.getchannel("A")
    cutout = image.convert("RGBA").copy()
    cutout.putalpha(alpha)
    return cutout


def inspect_cutout(original, cutout, white, options):
    """Screen actual outer edges and dimensions; flags are reasons to review, not verdicts."""
    import cv2
    import numpy as np
    warnings = list(cutout.info.get("auto_warnings", []))
    if white.size != original.size:
        warnings.append(f"尺寸变化：原图 {original.width}×{original.height}，输出 {white.width}×{white.height}")
    alpha_image = cutout.getchannel("A").copy()
    alpha_image.thumbnail((512, 512), Image.Resampling.BILINEAR)
    alpha = np.asarray(alpha_image)
    mask = (alpha >= 128).astype(np.uint8)
    if not mask.any():
        return warnings
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    main_index = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    main = (labels == main_index).astype(np.uint8)
    contours, _ = cv2.findContours(main, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return warnings
    contour = max(contours, key=cv2.contourArea)
    # Fill holes for analysis so intentional/internal holes do not trigger edge review.
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [contour], -1, 1, cv2.FILLED)
    band_kernel = np.ones((17, 17), np.uint8)
    band = cv2.dilate(filled, band_kernel) - cv2.erode(filled, band_kernel)
    points = contour[:, 0, :]
    height, width = mask.shape
    valid = (points[:, 0] > 1) & (points[:, 0] < width - 2) & (points[:, 1] > 1) & (points[:, 1] < height - 2)
    visible_points = points[valid]
    perimeter = cv2.arcLength(contour, True)
    # Thick soft transitions around the external contour, excluding image boundaries.
    band[:2] = 0; band[-2:] = 0; band[:, :2] = 0; band[:, -2:] = 0
    soft_count = np.count_nonzero((alpha >= 32) & (alpha <= 223) & (band != 0))
    transition_width = soft_count / max(1, perimeter)
    if transition_width > 3.0 and soft_count > 48:
        warnings.append("边缘不清晰：外轮廓过度羽化或存在较宽半透明边缘")
    if len(contour) >= 80:
        curve = contour.astype(np.float32)
        wrapped = np.pad(curve, ((4, 4), (0, 0), (0, 0)), mode="wrap")
        smooth = cv2.GaussianBlur(wrapped, (1, 9), 2)[4:-4]
        ratios = [cv2.arcLength(curve[i:i + 32], False) /
                  max(1, cv2.arcLength(smooth[i:i + 32], False))
                  for i in range(0, len(curve) - 32, 16)]
        rough = sum(ratio > 1.35 for ratio in ratios)
        if rough >= 2 and rough / max(1, len(ratios)) > 0.06:
            warnings.append("轮廓异常：外边缘存在较多锯齿、毛刺或破碎凹缺")
    # Only small fragments next to the main outline are suspected remnants.
    nearby = cv2.dilate(filled, np.ones((25, 25), np.uint8))
    main_area = int(stats[main_index, cv2.CC_STAT_AREA])
    for index in range(1, count):
        if index == main_index:
            continue
        area = int(stats[index, cv2.CC_STAT_AREA])
        if 6 <= area <= max(12, main_area * 0.02) and np.any((labels == index) & (nearby != 0)):
            warnings.append("背景残留：主体轮廓旁有小块分离残留，请检查是否未抠干净")
            break
    # A mask cutting across smooth original regions may miss an edge or include background.
    # Real contours and faint/blurred source boundaries can also trigger this conservative rule.
    if len(visible_points) >= 24:
        rgb = np.asarray(original.convert("RGB").resize((width, height), Image.Resampling.LANCZOS))
        gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
        gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
        strength = cv2.magnitude(gx, gy) / 8.0
        strength = cv2.dilate(strength, np.ones((5, 5), np.uint8))
        values = strength[visible_points[:, 1], visible_points[:, 0]]
        if np.mean(values < 6.0) > 0.45:
            warnings.append("疑似残留或缺边：较多输出轮廓与原图明显边缘不吻合，请对照外框检查")
    return list(dict.fromkeys(warnings))


def batch_cache(destination):
    base = Path(os.environ.get("WHITEBG_DATA_HOME", str(Path.home() / "Library" / "Application Support" / "WhiteBackgroundBatch")))
    key = hashlib.sha256(str(Path(destination).resolve()).encode()).hexdigest()
    return base / "batches" / key


def run_batch(source, destination, options=None, emit=None, cancel=None,
              session_factory=make_session, remover=remove_background):
    from workflow import run_batch as human_batch
    return human_batch(source, destination, options, emit, cancel, session_factory, remover)
