"""Native Tk interface. All image work runs off the UI thread."""
from __future__ import annotations

import json
import csv
from concurrent.futures import ThreadPoolExecutor
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
import math

from PIL import Image, ImageTk
from tkinter import filedialog, messagebox, ttk

from core import Options, VERSION, atomic_json, run_batch, batch_cache, digest, load_image

CONFIG_PATH = Path.home() / "Library" / "Application Support" / "WhiteBackgroundBatch" / "settings.json"
MODEL_LABELS = {"装裱刺绣保护 · 双模型": "framed-dual", "通用 · IS-Net": "isnet-general-use", "标准 · U²-Net": "u2net", "轻量试跑 · U²-Netp": "u2netp"}


class App:
    def __init__(self, root):
        self.root = root
        root.title("批量白底 · Mac")
        root.geometry("880x780")
        root.minsize(780, 680)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.closing = False
        self.started = 0
        self.worker_thread = None
        self.active_destination = None
        self.review_window = None
        self.saved = self.load_settings()
        self.source = tk.StringVar(value=self.saved.get("source", ""))
        self.destination = tk.StringVar(value=self.saved.get("destination", ""))
        self.model = tk.StringVar(value=self.saved.get("model", next(iter(MODEL_LABELS))))
        if self.model.get() not in MODEL_LABELS:
            self.model.set(next(iter(MODEL_LABELS)))
        self.layout = tk.StringVar(value=self.saved.get("layout", "original"))
        self.size = tk.StringVar(value=str(self.saved.get("size", 1500)))
        self.occupancy = tk.StringVar(value=str(self.saved.get("occupancy", 85)))
        self.fmt = tk.StringVar(value=self.saved.get("fmt", "jpg"))
        self.recursive = tk.BooleanVar(value=self.saved.get("recursive", True))
        self.resume = tk.BooleanVar(value=self.saved.get("resume", True))
        self.matting = tk.BooleanVar(value=self.saved.get("matting", False))
        self.status = tk.StringVar(value="先用 5–10 张试跑，再处理整个文件夹")
        self.count = tk.StringVar(value="准备就绪")
        self.build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def load_settings(self):
        try:
            value = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            if value.get("version") is None:
                value.update(layout="original", fmt="jpg", matting=False, model=next(iter(MODEL_LABELS)))
            value.pop("transparent", None)
            return value
        except (OSError, ValueError):
            return {}

    def build(self):
        style = ttk.Style()
        style.configure("Title.TLabel", font=("Helvetica", 25, "bold"))
        style.configure("Muted.TLabel", foreground="#687385")
        outer = ttk.Frame(self.root, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="批量白底", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="整个文件夹一次处理 · 原图保留 · 本地运行", style="Muted.TLabel").pack(anchor="w", pady=(4, 18))
        paths = ttk.LabelFrame(outer, text="01  选择文件夹", padding=12)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)
        for row, (label, variable, command) in enumerate([
            ("原图位置", self.source, self.pick_source), ("输出位置", self.destination, self.pick_destination)
        ]):
            ttk.Label(paths, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=6)
            ttk.Entry(paths, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=6)
            ttk.Button(paths, text="选择…", command=command).grid(row=row, column=2, padx=(10, 0), pady=6)
        settings = ttk.LabelFrame(outer, text="02  输出设置", padding=12)
        settings.pack(fill="x", pady=14)
        ttk.Label(settings, text="抠图模型").grid(row=0, column=0, sticky="w")
        model_box = ttk.Combobox(settings, textvariable=self.model, values=list(MODEL_LABELS), state="readonly", width=23)
        model_box.grid(row=0, column=1, sticky="w", padx=8)
        model_box.bind("<<ComboboxSelected>>", self.update_model_controls)
        ttk.Label(settings, text="白底格式").grid(row=0, column=2, sticky="w", padx=(20, 8))
        ttk.Combobox(settings, textvariable=self.fmt, values=["jpg", "png"], state="readonly", width=7).grid(row=0, column=3, sticky="w")
        ttk.Radiobutton(settings, text="统一正方形（尺寸变化会待复核）", variable=self.layout, value="square").grid(row=1, column=0, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Radiobutton(settings, text="保持原图尺寸及位置", variable=self.layout, value="original").grid(row=1, column=2, columnspan=2, sticky="w", pady=(12, 0))
        ttk.Label(settings, text="正方形边长").grid(row=2, column=0, sticky="w", pady=10)
        ttk.Combobox(settings, textvariable=self.size, values=[1000, 1500, 2000, 3000], width=10).grid(row=2, column=1, sticky="w", padx=8, pady=10)
        ttk.Label(settings, text="主体最长边占比").grid(row=2, column=2, sticky="w", padx=(20, 8))
        ttk.Spinbox(settings, textvariable=self.occupancy, from_=50, to=95, width=7).grid(row=2, column=3, sticky="w")
        toggles = ttk.Frame(settings)
        toggles.grid(row=3, column=0, columnspan=4, sticky="w")
        for variable, label in [(self.recursive, "包含子文件夹"), (self.resume, "跳过已完成图片")]:
            ttk.Checkbutton(toggles, text=label, variable=variable).pack(side="left", padx=(0, 16))
        self.matting_box = ttk.Checkbutton(settings, text="细边优化（更慢；刺绣、装裱等不透明物品先关闭）", variable=self.matting)
        self.matting_box.grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.update_model_controls()
        ttk.Label(settings, text="装裱保护：双模型较慢，填补内部误抠；仅用于单件实心凸形物品\n镂空、玻璃、多件物品请选择通用模型；装裱模式不使用细边优化", style="Muted.TLabel").grid(row=5, column=0, columnspan=4, sticky="w", pady=(8, 0))
        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 12))
        self.start_button = ttk.Button(controls, text="开始批量处理", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="停止（完成当前图片后）", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=10)
        ttk.Button(controls, text="打开输出文件夹", command=self.open_output).pack(side="right")
        ttk.Button(controls, text="打开原因清单", command=self.open_report).pack(side="right", padx=8)
        review_controls = ttk.Frame(outer)
        review_controls.pack(fill="x", pady=(0, 10))
        ttk.Button(review_controls, text="原图与结果对照", command=self.open_review).pack(side="left")
        ttk.Label(review_controls, text="同步放大与拖动 · 可记录人工判断供后续校准", style="Muted.TLabel").pack(side="left", padx=10)
        ttk.Label(outer, textvariable=self.status, wraplength=810).pack(anchor="w")
        self.progress = ttk.Progressbar(outer, mode="determinate")
        self.progress.pack(fill="x", pady=10)
        ttk.Label(outer, textvariable=self.count, style="Muted.TLabel").pack(anchor="w")
        self.log = tk.Text(outer, height=7, wrap="word", state="disabled", relief="flat", background="#f0f2f5", foreground="#303747", padx=10, pady=8)
        self.log.pack(fill="both", expand=True, pady=(12, 8))
        ttk.Label(outer, text=f"v{VERSION}  ·  输出仅两个文件夹；自查尺寸与外轮廓，自动通过仍须抽查", style="Muted.TLabel").pack(anchor="w")
        self.input_widgets = []
        def collect(parent):
            for widget in parent.winfo_children():
                if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Spinbox, ttk.Checkbutton, ttk.Radiobutton, ttk.Button)):
                    self.input_widgets.append((widget, widget.cget("state")))
                collect(widget)
        collect(paths)
        collect(settings)

    def update_model_controls(self, event=None):
        framed = MODEL_LABELS.get(self.model.get()) == "framed-dual"
        if framed:
            self.matting.set(False)
        self.matting_box.configure(state="disabled" if framed else "normal")

    def pick_source(self):
        selected = filedialog.askdirectory(title="选择原图文件夹")
        if selected:
            self.source.set(selected)
            if not self.destination.get():
                source = Path(selected)
                self.destination.set(str(source.parent / (source.name + "_白底结果")))

    def pick_destination(self):
        selected = filedialog.askdirectory(title="选择输出文件夹（建议使用空文件夹）", mustexist=False)
        if selected:
            self.destination.set(selected)

    def start(self):
        if self.running:
            return
        try:
            if not self.source.get().strip() or not self.destination.get().strip():
                raise ValueError("请先选择原图文件夹和输出文件夹")
            source = Path(self.source.get()).expanduser().resolve()
            destination = Path(self.destination.get()).expanduser().resolve()
            if not source.is_dir():
                raise ValueError("原图文件夹不存在")
            if destination == source or source.is_relative_to(destination):
                raise ValueError("输出位置不能与原图相同，也不能是原图的上级文件夹")
            options = Options(model=MODEL_LABELS[self.model.get()], layout=self.layout.get(), size=int(self.size.get()),
                              occupancy=float(self.occupancy.get()) / 100, white_format=self.fmt.get(),
                              recursive=self.recursive.get(), resume=self.resume.get(),
                              alpha_matting=self.matting.get() and MODEL_LABELS[self.model.get()] != "framed-dual")
            options.validate()
            atomic_json(CONFIG_PATH, {"version": VERSION, "source": str(source), "destination": str(destination), "model": self.model.get(),
                                      "layout": options.layout, "size": options.size, "occupancy": round(options.occupancy * 100),
                                      "fmt": options.white_format, "recursive": options.recursive,
                                      "resume": options.resume, "matting": options.alpha_matting})
        except Exception as exc:
            messagebox.showerror("设置需要调整", str(exc))
            return
        self.active_destination = destination
        self.cancel.clear()
        self.running = True
        self.started = time.monotonic()
        self.progress.configure(value=0, maximum=1)
        self.count.set("正在扫描原图…")
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        for widget, _ in self.input_widgets:
            widget.configure(state="disabled")
        self.append_log("开始处理。安装和模型下载需要联网；图片抠图在本机完成。")
        def work():
            try:
                run_batch(source, destination, options, lambda event, value: self.events.put((event, value)), self.cancel)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("finished", None))
        self.worker_thread = threading.Thread(target=work, daemon=False)
        self.worker_thread.start()

    def stop(self):
        self.cancel.set()
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止：等当前图片或模型准备完成，已保存结果可在下次继续")

    def append_log(self, value):
        self.log.configure(state="normal")
        self.log.insert("end", value + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == "total":
                    self.progress.configure(maximum=value)
                elif event == "status":
                    if not self.cancel.is_set():
                        self.status.set(value)
                elif event == "progress":
                    self.progress.configure(value=value)
                    seconds = int(time.monotonic() - self.started)
                    self.count.set(f"已检查 {value}/{int(self.progress['maximum'])} 张 · 已用时 {seconds // 60} 分 {seconds % 60} 秒")
                elif event == "log":
                    self.append_log(value)
                elif event == "done":
                    summary = f"{'已停止' if value['cancelled'] else '处理结束'}：完成 {value['success']} · 跳过 {value['skipped']} · 失败 {value['failed']} · 自动通过 {value['auto_pass']} · 待人工 {value['review']}"
                    self.status.set(summary)
                    self.append_log(summary + "。点击“打开原因清单”，查看可疑图片的原因。")
                elif event == "error":
                    self.status.set("处理遇到问题，请查看下面的错误")
                    self.append_log(value)
                    messagebox.showerror("处理未完成", value)
                elif event == "finished":
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    for widget, initial_state in self.input_widgets:
                        widget.configure(state=initial_state)
                    self.update_model_controls()
                    if self.closing:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def output_path(self):
        return self.active_destination if self.running else Path(self.destination.get()).expanduser()

    def open_output(self):
        path = self.output_path()
        if path and path.is_dir():
            subprocess.Popen(["open", str(path)])
        else:
            messagebox.showinfo("还没有输出", "选择输出位置并运行一次后即可打开")

    def open_report(self):
        path = self.output_path() / "待人工复核" / "复核原因.csv"
        if path.is_file():
            subprocess.Popen(["open", str(path)])
        else:
            messagebox.showinfo("还没有原因清单", "完成第一张图片后即可查看")

    def open_review(self):
        if self.review_window is not None and self.review_window.window.winfo_exists():
            self.review_window.window.lift()
            return
        try:
            source, rows = review_entries(self.output_path())
            if not rows:
                messagebox.showinfo("还没有结果", "完成图片处理后即可对照查看")
                return
            self.review_window = ReviewWindow(self.root, self.output_path(), source, rows)
        except Exception as exc:
            messagebox.showerror("无法打开对照", str(exc))

    def close(self):
        if self.running:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()



def review_path(root, relative):
    """CSV and cached paths cannot escape their known root."""
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("图片路径超出了对应文件夹")
    return path


def review_entries(destination):
    destination = Path(destination).resolve()
    try:
        state = json.loads((batch_cache(destination) / "续跑记录.json").read_text(encoding="utf-8"))
        source = Path(state["source_root"])
    except (OSError, ValueError, KeyError) as exc:
        raise ValueError("找不到该批次的原图记录，请在原输出位置完成一次处理或续跑") from exc
    rows = {}
    for key, entry in state.get("files", {}).items():
        row = dict(entry.get("row", {}))
        white = row.get("white")
        if white and review_path(destination, white).is_file():
            row.update(source=key, source_hash=entry.get("source_hash"),
                       output_hash=entry.get("outputs", {}).get(white), config=entry.get("config"))
            rows[key] = row
    reason_path = destination / "待人工复核" / "复核原因.csv"
    if reason_path.is_file():
        with reason_path.open(encoding="utf-8-sig", newline="") as stream:
            for item in list(csv.reader(stream))[1:]:
                if len(item) != 4:
                    continue
                key, status, reason, white = item
                row = rows.setdefault(key, {"source": key})
                row.update(status=status, note=reason, white=white, quality="待人工复核")
    for row in rows.values():
        review_path(source, row["source"])
        if row.get("white"):
            review_path(destination, row["white"])
    return source, [rows[key] for key in sorted(rows)]


def viewport_image(image, size, zoom, center):
    """Render a shared pixel-coordinate viewport without enlarging the whole photo."""
    width, height = size
    if width < 1 or height < 1 or zoom <= 0:
        raise ValueError("显示尺寸与缩放倍率必须大于零")
    canvas = Image.new("RGB", size, "#edf0f3")
    left = center[0] - width / (2 * zoom)
    top = center[1] - height / (2 * zoom)
    box = (max(0, math.floor(left)), max(0, math.floor(top)),
           min(image.width, math.ceil(left + width / zoom)),
           min(image.height, math.ceil(top + height / zoom)))
    if box[2] <= box[0] or box[3] <= box[1]:
        return canvas
    part = image.crop(box).convert("RGBA")
    target = (max(1, round(part.width * zoom)), max(1, round(part.height * zoom)))
    if part.size != target:
        part = part.resize(target, Image.Resampling.LANCZOS)
    offset = (round((box[0] - left) * zoom), round((box[1] - top) * zoom))
    canvas.paste(part, offset, part)
    return canvas


def save_review_label(destination, row, label, note, hashes):
    if label not in {"acceptable", "needs_edit"} or not hashes.get("original") or not hashes.get("white"):
        raise ValueError("请等待两张图片载入后再标记")
    # A label describes these exact bytes, not a later rerun or an altered original.
    if digest(review_path(destination, row["white"])) != hashes["white"]:
        raise ValueError("结果图已经改变，请关闭并重新打开对照后再标记")
    cache_path = batch_cache(destination) / "人工校准.json"
    try:
        saved = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        saved = {"schema": 1, "labels": {}}
    saved["labels"][row["source"]] = {
        "label": label, "note": note, "source_hash": hashes["original"],
        "output_hash": hashes["white"], "config": row.get("config"),
        "automatic_quality": row.get("quality"), "automatic_reason": row.get("note", ""),
        "white": row["white"]}
    atomic_json(cache_path, saved)


def calibration_counts(destination):
    """Counts describe labelled outputs matching the current batch records."""
    cache = batch_cache(destination)
    try:
        state = json.loads((cache / "续跑记录.json").read_text(encoding="utf-8"))
        saved = json.loads((cache / "人工校准.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"labelled": 0, "false_positive": 0, "false_negative": 0, "stale": 0}
    counts = {"labelled": 0, "false_positive": 0, "false_negative": 0, "stale": 0}
    for key, label in saved.get("labels", {}).items():
        entry = state.get("files", {}).get(key, {})
        if (label.get("label") not in {"acceptable", "needs_edit"}
                or label.get("source_hash") != entry.get("source_hash")
                or label.get("output_hash") != entry.get("outputs", {}).get(label.get("white"))
                or label.get("config") != entry.get("config")):
            counts["stale"] += 1
            continue
        counts["labelled"] += 1
        flagged = label.get("automatic_quality") != "自动检查通过（需抽查）"
        counts["false_positive"] += int(flagged and label["label"] == "acceptable")
        counts["false_negative"] += int(not flagged and label["label"] == "needs_edit")
    return counts


class ReviewWindow:
    def __init__(self, parent, destination, source, rows):
        self.destination, self.source, self.rows = Path(destination), Path(source), rows
        self.window = tk.Toplevel(parent)
        self.window.title("原图与结果对照")
        self.window.geometry("1250x830")
        self.window.minsize(900, 650)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.queue = queue.Queue()
        self.loader = ThreadPoolExecutor(max_workers=1)
        self.load_future = None
        self.generation = 0
        self.images, self.hashes, self.photos = {}, {}, []
        self.current = None
        self.zoom, self.center = 1.0, (0, 0)
        self.render_job = None
        self.filter = tk.StringVar(value="只看待人工复核")
        self.detail = tk.StringVar()
        self.position = tk.StringVar()
        self.note = tk.StringVar()
        self.judgement = tk.StringVar(value="尚未人工标记")
        self.calibration = tk.StringVar()
        outer = ttk.Frame(self.window, padding=12)
        outer.pack(fill="both", expand=True)
        bar = ttk.Frame(outer); bar.pack(fill="x")
        box = ttk.Combobox(bar, textvariable=self.filter, values=["只看待人工复核", "全部结果"], state="readonly", width=18)
        box.pack(side="left"); box.bind("<<ComboboxSelected>>", self.fill_list)
        for title, command in [("刷新", self.refresh), ("上一张", lambda: self.navigate(-1)), ("下一张", lambda: self.navigate(1)),
                               ("适应窗口", self.fit), ("100%", lambda: self.set_zoom(1)),
                               ("缩小", lambda: self.set_zoom(self.zoom / 1.25)),
                               ("放大", lambda: self.set_zoom(self.zoom * 1.25))]:
            ttk.Button(bar, text=title, command=command).pack(side="left", padx=3)
        ttk.Label(bar, textvariable=self.position).pack(side="right")
        body = ttk.Frame(outer); body.pack(fill="both", expand=True, pady=10)
        side = ttk.Frame(body); side.pack(side="left", fill="y", padx=(0, 10))
        self.listbox = tk.Listbox(side, width=28, exportselection=False)
        self.listbox.pack(side="left", fill="both", expand=True)
        scroll = ttk.Scrollbar(side, orient="vertical", command=self.listbox.yview)
        scroll.pack(side="right", fill="y"); self.listbox.configure(yscrollcommand=scroll.set)
        self.listbox.bind("<<ListboxSelect>>", self.select)
        images = ttk.Frame(body); images.pack(side="left", fill="both", expand=True)
        images.columnconfigure(0, weight=1, uniform="image"); images.columnconfigure(1, weight=1, uniform="image")
        images.rowconfigure(1, weight=1)
        self.canvases, self.titles = [], []
        for index, label in enumerate(["原图", "白底结果"]):
            title = ttk.Label(images, text=label); title.grid(row=0, column=index, pady=(0, 5))
            canvas = tk.Canvas(images, background="#edf0f3", highlightthickness=0)
            canvas.grid(row=1, column=index, sticky="nsew", padx=3)
            canvas.bind("<Configure>", self.schedule_render)
            canvas.bind("<ButtonPress-1>", self.drag_start)
            canvas.bind("<B1-Motion>", self.drag)
            canvas.bind("<MouseWheel>", self.wheel)
            canvas.bind("<Button-4>", lambda event: self.set_zoom(self.zoom * 1.15))
            canvas.bind("<Button-5>", lambda event: self.set_zoom(self.zoom / 1.15))
            self.canvases.append(canvas); self.titles.append(title)
        ttk.Label(outer, textvariable=self.detail, wraplength=820).pack(anchor="w")
        ttk.Label(outer, text="两侧使用相同像素比例；拖动任一图片同步移动。100% 表示一个图片像素对应一个 Tk 显示单位。", foreground="#687385").pack(anchor="w", pady=5)
        feedback = ttk.Frame(outer); feedback.pack(fill="x", pady=5)
        ttk.Label(feedback, text="人工备注").pack(side="left")
        ttk.Entry(feedback, textvariable=self.note).pack(side="left", fill="x", expand=True, padx=8)
        self.label_buttons = []
        for title, label in [("无需修整", "acceptable"), ("需要修整", "needs_edit")]:
            button = ttk.Button(feedback, text=title, command=lambda value=label: self.mark(value))
            button.pack(side="left", padx=3); self.label_buttons.append(button)
        ttk.Label(outer, textvariable=self.judgement).pack(anchor="w")
        ttk.Label(outer, textvariable=self.calibration).pack(anchor="w")
        ttk.Label(outer, text="人工标记仅保存在工具内部，供后续校准；不改变复核规则、不移动图片、不修改原图。", foreground="#687385").pack(anchor="w", pady=(5, 0))
        self.fill_list()
        self.show_calibration()
        self.window.after(100, self.poll)

    def refresh(self):
        try:
            self.source, self.rows = review_entries(self.destination)
            self.fill_list()
            self.show_calibration()
        except Exception as exc:
            messagebox.showerror("无法刷新", str(exc), parent=self.window)

    def fill_list(self, event=None):
        self.visible = [row for row in self.rows if self.filter.get() == "全部结果" or row.get("quality") != "自动检查通过（需抽查）"]
        self.listbox.delete(0, "end")
        for row in self.visible:
            self.listbox.insert("end", row["source"])
        if self.visible:
            self.listbox.selection_set(0); self.select()
        else:
            self.generation += 1
            self.images, self.photos, self.hashes = {}, [], {}
            self.current = None
            self.detail.set("当前没有待人工复核的项目，可切换为全部结果进行抽查。")
            self.position.set("0 张"); self.note.set(""); self.judgement.set("尚未人工标记")
            self.schedule_render()
            self.enable_labels(False)

    def navigate(self, step):
        if not self.visible:
            return
        selected = self.listbox.curselection()
        index = min(len(self.visible) - 1, max(0, (selected[0] if selected else 0) + step))
        self.listbox.selection_clear(0, "end"); self.listbox.selection_set(index)
        self.listbox.see(index); self.select()

    def enable_labels(self, enabled):
        for button in self.label_buttons:
            button.configure(state="normal" if enabled else "disabled")

    def select(self, event=None):
        selection = self.listbox.curselection()
        if not selection:
            return
        index = selection[0]; self.current = self.visible[index]
        self.generation += 1; generation = self.generation
        self.images, self.hashes, self.photos = {}, {}, []
        self.note.set(""); self.judgement.set("正在载入…")
        self.detail.set(self.current.get("note") or "未触发自动筛查规则，仍需抽查。")
        self.position.set(f"{index + 1}/{len(self.visible)}")
        self.enable_labels(False); self.schedule_render()
        row = dict(self.current)
        def work():
            loaded, hashes, errors = {}, {}, []
            for kind, base, relative in [("original", self.source, row["source"]), ("white", self.destination, row.get("white"))]:
                if not relative:
                    errors.append("没有生成可疑图片"); continue
                try:
                    path = review_path(base, relative)
                    before_hash = digest(path)
                    image = load_image(path)
                    after_hash = digest(path)
                    if before_hash != after_hash:
                        raise ValueError("文件在载入时发生改变，请重新打开对照")
                    loaded[kind] = image
                    hashes[kind] = after_hash
                except Exception as exc:
                    errors.append(f"{'原图' if kind == 'original' else '结果图'}无法载入：{exc}")
            self.queue.put((generation, loaded, hashes, errors))
        if self.load_future is not None:
            self.load_future.cancel()
        self.load_future = self.loader.submit(work)

    def poll(self):
        try:
            while True:
                generation, loaded, hashes, errors = self.queue.get_nowait()
                if generation != self.generation:
                    continue
                self.images, self.hashes = loaded, hashes
                for index, kind in enumerate(["original", "white"]):
                    im = loaded.get(kind)
                    self.titles[index].configure(text=("原图" if index == 0 else "白底结果") + (f" · {im.width}×{im.height}" if im else " · 无图片"))
                if errors:
                    self.detail.set(self.detail.get() + "；" + "；".join(errors))
                changed = (self.current.get("source_hash") and hashes.get("original") != self.current["source_hash"]) or (self.current.get("output_hash") and hashes.get("white") != self.current["output_hash"])
                if changed:
                    self.detail.set(self.detail.get() + "；文件已发生变化，无法用于本次校准，请重新处理后查看。")
                self.enable_labels(len(loaded) == 2 and len(hashes) == 2 and not changed)
                self.restore_label(); self.fit()
        except queue.Empty:
            pass
        if self.window.winfo_exists():
            self.window.after(100, self.poll)

    def restore_label(self):
        self.judgement.set("尚未人工标记")
        try:
            saved = json.loads((batch_cache(self.destination) / "人工校准.json").read_text(encoding="utf-8"))
            value = saved["labels"][self.current["source"]]
            if value["source_hash"] == self.hashes.get("original") and value["output_hash"] == self.hashes.get("white"):
                self.note.set(value.get("note", ""))
                self.judgement.set("已标记：" + ("无需修整" if value["label"] == "acceptable" else "需要修整"))
        except (OSError, ValueError, KeyError):
            pass

    def mark(self, label):
        try:
            if not self.current or len(self.images) != 2:
                return
            if digest(review_path(self.source, self.current["source"])) != self.hashes["original"]:
                raise ValueError("原图已经改变，请重新打开对照后再标记")
            save_review_label(self.destination, self.current, label, self.note.get(), self.hashes)
            self.restore_label()
            self.show_calibration()
        except Exception as exc:
            messagebox.showerror("无法保存标记", str(exc), parent=self.window)

    def show_calibration(self):
        try:
            counts = calibration_counts(self.destination)
            self.calibration.set(f"已标记 {counts['labelled']} 张 · 误报 {counts['false_positive']} 张 · 漏报 {counts['false_negative']} 张（仅统计已标记、与续跑记录一致的结果）")
        except (OSError, ValueError, KeyError):
            self.calibration.set("校准记录暂时无法读取；当前自动规则不受影响。")

    def fit(self):
        if not self.images:
            return
        width = max(image.width for image in self.images.values())
        height = max(image.height for image in self.images.values())
        self.center = (width / 2, height / 2)
        self.set_zoom(min(min(max(1, c.winfo_width()) / width, max(1, c.winfo_height()) / height) for c in self.canvases))

    def set_zoom(self, zoom):
        self.zoom = max(0.01, min(8, zoom)); self.schedule_render()

    def wheel(self, event):
        if event.delta:
            self.set_zoom(self.zoom * (1.15 if event.delta > 0 else 1 / 1.15))

    def drag_start(self, event):
        self.drag_origin = (event.x, event.y, self.center)

    def drag(self, event):
        if not hasattr(self, "drag_origin"):
            return
        x, y, center = self.drag_origin
        self.center = (center[0] - (event.x - x) / self.zoom, center[1] - (event.y - y) / self.zoom)
        self.schedule_render()

    def schedule_render(self, event=None):
        if self.render_job is not None:
            self.window.after_cancel(self.render_job)
        self.render_job = self.window.after(40, self.render)

    def render(self):
        self.render_job = None; self.photos = []
        for canvas, kind in zip(self.canvases, ["original", "white"]):
            canvas.delete("all")
            if kind not in self.images:
                canvas.create_text(max(1, canvas.winfo_width()) / 2, max(1, canvas.winfo_height()) / 2, text="暂无图片", fill="#687385")
                continue
            frame = viewport_image(self.images[kind], (max(1, canvas.winfo_width()), max(1, canvas.winfo_height())), self.zoom, self.center)
            photo = ImageTk.PhotoImage(frame, master=self.window)
            self.photos.append(photo); canvas.create_image(0, 0, anchor="nw", image=photo)
        if self.current and self.listbox.curselection():
            self.position.set(f"{self.listbox.curselection()[0] + 1}/{len(self.visible)} · {self.zoom * 100:.0f}%")

    def close(self):
        self.loader.shutdown(wait=False, cancel_futures=True)
        self.generation += 1
        if self.render_job is not None:
            self.window.after_cancel(self.render_job)
        self.window.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
