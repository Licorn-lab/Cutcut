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

VERSION = "1.5.0"
# Change only when image processing or quality screening changes.
PIPELINE_VERSION = "1.4.0"
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
MODELS = {"u2net", "u2netp", "isnet-general-use", "framed-dual"}


class ModelPreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Options:
    model: str = "framed-dual"
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
        if getattr(image, "n_frames", 1) > 1:
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
    warnings = []
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


def write_reports(destination, rows):
    """Export just suspicious/failed items; resume state stays in the internal cache."""
    report_root = destination / "待人工复核"
    report_root.mkdir(parents=True, exist_ok=True)
    pending = [r for r in rows if r.get("quality") != "自动检查通过（需抽查）"]
    columns = ("source", "status", "note", "white")
    headers = ["原图相对路径", "处理状态", "复核原因或错误", "可疑图片路径（相对输出位置）"]
    fd, tmp = tempfile.mkstemp(prefix=".report-", dir=report_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(headers)
            for row in pending:
                writer.writerow([row.get(k, "") for k in columns])
        os.replace(tmp, report_root / "复核原因.csv")
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run_batch(source, destination, options=None, emit=None, cancel=None,
              session_factory=make_session, remover=remove_background):
    options = options or Options()
    options.validate()
    emit = emit or (lambda event, value: None)
    cancel = cancel or threading.Event()
    source, destination = Path(source).resolve(), Path(destination).resolve()
    images = collect_images(source, destination, options.recursive)
    if not images:
        raise ValueError("文件夹中没有支持的静态图片")
    destination.mkdir(parents=True, exist_ok=True)
    allowed = {"白底图", "待人工复核"}
    unexpected = [p.name for p in destination.iterdir() if p.name != ".DS_Store" and
                  (p.name not in allowed or not p.is_dir() or p.is_symlink())]
    if unexpected:
        raise ValueError("输出位置含旧版文件或其他内容。为只保留两个文件夹，请选择新的空输出文件夹。")
    legacy_reports = {"处理清单.csv", "待人工复核.csv", "结果检查.html", "待人工复核.html"}
    if any((destination / "待人工复核" / name).exists() for name in legacy_reports):
        raise ValueError("待人工复核内含旧版清单或检查页。请选择新的空输出文件夹，旧文件不会删除。")
    cache = batch_cache(destination)
    cache.mkdir(parents=True, exist_ok=True)
    state_path = cache / "续跑记录.json"
    import fcntl
    lock = (cache / "batch.lock").open("a+")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("这个输出文件夹正在被另一个批处理使用") from exc
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
            if state.get("source_root") != str(source):
                raise ValueError("该输出位置已有另一批原图的记录，请选择新的输出文件夹")
            if not isinstance(state.get("files"), dict):
                raise ValueError("续跑记录格式错误，请选择新的输出文件夹")
        except FileNotFoundError:
            if any(p.suffix.lower() in EXTENSIONS for folder in allowed
                   if (destination / folder).is_dir() for p in (destination / folder).rglob("*")):
                raise ValueError("该输出位置已有图片，但续跑记录不可用，请选择新的空输出文件夹。")
            state = {"version": VERSION, "source_root": str(source), "files": {}}
        except json.JSONDecodeError as exc:
            raise ValueError("续跑记录损坏，请选择新的输出文件夹，旧结果仍可使用") from exc
        for folder in allowed:
            (destination / folder).mkdir(exist_ok=True)
        rows, session = [], None
        stats = {"success": 0, "skipped": 0, "failed": 0, "review": 0, "auto_pass": 0, "total": len(images), "cancelled": False}
        emit("total", len(images))
        config = options.signature()
        state["version"] = VERSION
        state["options"] = asdict(options)
        atomic_json(state_path, state)
        for index, path in enumerate(images, 1):
            if cancel.is_set():
                stats["cancelled"] = True
                break
            rel = path.relative_to(source)
            key = rel.as_posix()
            row = {"source": key, "status": "失败", "quality": "处理失败，需人工处理", "note": ""}
            white_rel = None
            try:
                emit("status", f"{index}/{len(images)} · {key}")
                source_hash = digest(path)
                previous = state["files"].get(key, {})
                files = previous.get("outputs", {})
                resume_ok = (options.resume and previous.get("config") == config
                             and previous.get("source_hash") == source_hash and files
                             and all((destination / name).is_file() and digest(destination / name) == value
                                     for name, value in files.items()))
                if resume_ok:
                    row = {**previous["row"], "status": "跳过（已完成）"}
                    stats["skipped"] += 1
                else:
                    if session is None:
                        emit("status", "正在准备本地模型，首次使用需要联网下载，请查看启动终端的进度")
                        try:
                            session = session_factory(options.model)
                        except Exception as exc:
                            raise ModelPreparationError(str(exc)) from exc
                    if cancel.is_set():
                        stats["cancelled"] = True
                        break
                    emit("status", f"{index}/{len(images)} · 正在抠图：{key}")
                    original = load_image(path)
                    cutout = remover(original, session, options)
                    if cutout.size != original.size:
                        raise ValueError("模型返回的尺寸与原图不一致")
                    alpha = ImageChops.multiply(cutout.getchannel("A"), original.getchannel("A"))
                    cutout = original.copy()
                    cutout.putalpha(alpha)
                    white, warnings = render_white(cutout, options)
                    warnings = list(dict.fromkeys(warnings + inspect_cutout(original, cutout, white, options)))
                    needs_review = bool(warnings)
                    white_folder = Path("待人工复核") if needs_review else Path("白底图")
                    white_rel = white_folder / rel.parent / (rel.name + ".white." + options.white_format)
                    save_image_atomic(white, destination / white_rel,
                                      "JPEG" if options.white_format == "jpg" else "PNG",
                                      **({"quality": 100, "subsampling": 0} if options.white_format == "jpg" else {}))
                    with Image.open(destination / white_rel) as saved:
                        saved.load()
                        if saved.size != white.size:
                            warnings.append(f"保存后尺寸异常：原图 {original.width}×{original.height}，文件 {saved.width}×{saved.height}")
                            needs_review = True
                            white = saved.convert("RGB")
                    if needs_review and white_rel.parts[0] != "待人工复核":
                        review_rel = Path("待人工复核") / rel.parent / white_rel.name
                        (destination / review_rel).parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination / white_rel, destination / review_rel)
                        white_rel = review_rel
                    old_name = previous.get("row", {}).get("white")
                    if old_name and old_name != white_rel.as_posix():
                        old_path = destination / old_name
                        known_hash = previous.get("outputs", {}).get(old_name)
                        if (old_path.resolve().is_relative_to(destination) and old_path.is_file()
                                and known_hash and digest(old_path) == known_hash):
                            old_path.unlink()
                    outputs = [white_rel]
                    row = {"source": key, "status": "完成", "quality": "待人工复核" if needs_review else "自动检查通过（需抽查）", "note": "；".join(warnings),
                           "white": white_rel.as_posix()}
                    state["files"][key] = {"config": config, "source_hash": source_hash,
                                           "outputs": {p.as_posix(): digest(destination / p) for p in outputs}, "row": row}
                    atomic_json(state_path, state)
                    stats["success"] += 1
            except Exception as exc:
                row["note"] = f"{type(exc).__name__}: {exc}"
                if white_rel is not None and (destination / white_rel).is_file():
                    review_rel = Path("待人工复核") / rel.parent / white_rel.name
                    if review_rel != white_rel:
                        (destination / review_rel).parent.mkdir(parents=True, exist_ok=True)
                        os.replace(destination / white_rel, destination / review_rel)
                    row["white"] = review_rel.as_posix()
                stats["failed"] += 1
                emit("log", f"失败：{key} · {row['note']}")
                # A model initialization/download failure affects all images; report once and stop.
                if isinstance(exc, ModelPreparationError):
                    stats["review"] += 1
                    rows.append(row)
                    write_reports(destination, rows)
                    raise RuntimeError("模型准备失败。请检查网络、磁盘空间及启动终端的错误；重新运行可继续。") from exc
            if row.get("quality") == "自动检查通过（需抽查）":
                stats["auto_pass"] += 1
            else:
                stats["review"] += 1
            rows.append(row)
            if index == 1 or index % 20 == 0:
                write_reports(destination, rows)
            emit("progress", index)
            emit("row", row)
        write_reports(destination, rows)
        emit("done", stats)
        return stats
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
