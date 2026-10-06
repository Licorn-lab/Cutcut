"""Native Tk interface. All image work runs off the UI thread."""
from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from core import Options, VERSION, atomic_json, run_batch

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

    def close(self):
        if self.running:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
