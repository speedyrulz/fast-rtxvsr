"""Desktop GUI for fast-rtxvsr (tkinter, no extra dependencies).

``fast-rtxvsr gui`` opens a small window: queue videos and images, pick the
output size (fixed WxH or a scale factor for images), quality, codec and
output folder, then Run. The GUI shells out to ``fast-rtxvsr run`` exactly as
a script would and streams its JSON events into the log, so everything the
CLI can do the GUI does the same way.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .env import repo_root, resolve_python
from .vsr import IMAGE_EXTS

VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"})
QUALITIES = ("LOW", "MEDIUM", "HIGH", "ULTRA")
CODECS = ("h264", "hevc", "av1")
PRESETS = tuple(f"P{i}" for i in range(1, 8))
SIZE_PRESETS = {
    "1920x1080 (1080p)": (1920, 1080),
    "1080x1920 (1080p portrait)": (1080, 1920),
    "2560x1440 (1440p)": (2560, 1440),
    "3840x2160 (4K)": (3840, 2160),
    "2160x3840 (4K portrait)": (2160, 3840),
}


def _open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # noqa: S606
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"fast-rtxvsr {__version__}")
        self.minsize(760, 560)
        self._proc: subprocess.Popen | None = None
        self._events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._files: list[Path] = []
        self._last_outputs: list[Path] = []
        self._build()
        self.after(100, self._drain)

    # ---------------------------------------------------------------- layout
    def _build(self) -> None:
        pad = {"padx": 6, "pady": 3}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.rowconfigure(2, weight=2)

        # -- input queue
        files = ttk.LabelFrame(root, text="Inputs (videos and images)")
        files.grid(row=0, column=0, sticky="nsew", **pad)
        files.columnconfigure(0, weight=1)
        files.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(files, selectmode="extended", height=8)
        self.listbox.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        scroll = ttk.Scrollbar(files, orient="vertical", command=self.listbox.yview)
        scroll.grid(row=0, column=1, sticky="ns", pady=6)
        self.listbox.configure(yscrollcommand=scroll.set)
        buttons = ttk.Frame(files)
        buttons.grid(row=0, column=2, sticky="n", padx=6, pady=6)
        ttk.Button(buttons, text="Add files...", command=self._add_files).pack(fill="x")
        ttk.Button(buttons, text="Add folder...", command=self._add_folder).pack(fill="x", pady=3)
        ttk.Button(buttons, text="Remove", command=self._remove_selected).pack(fill="x")
        ttk.Button(buttons, text="Clear", command=self._clear).pack(fill="x", pady=3)

        # -- options
        opts = ttk.LabelFrame(root, text="Options")
        opts.grid(row=1, column=0, sticky="ew", **pad)
        for col in range(6):
            opts.columnconfigure(col, weight=1 if col in (1, 3, 5) else 0)

        self.size_mode = tk.StringVar(value="fixed")
        ttk.Radiobutton(
            opts, text="Output size", variable=self.size_mode, value="fixed",
            command=self._sync_size_mode,
        ).grid(row=0, column=0, sticky="w", **pad)
        size_row = ttk.Frame(opts)
        size_row.grid(row=0, column=1, columnspan=5, sticky="w", **pad)
        self.width = tk.StringVar(value="1920")
        self.height = tk.StringVar(value="1080")
        self.width_entry = ttk.Entry(size_row, textvariable=self.width, width=7)
        self.width_entry.pack(side="left")
        ttk.Label(size_row, text="x").pack(side="left", padx=3)
        self.height_entry = ttk.Entry(size_row, textvariable=self.height, width=7)
        self.height_entry.pack(side="left")
        self.size_preset = ttk.Combobox(
            size_row, values=list(SIZE_PRESETS), state="readonly", width=26
        )
        self.size_preset.pack(side="left", padx=8)
        self.size_preset.bind("<<ComboboxSelected>>", self._apply_size_preset)

        ttk.Radiobutton(
            opts, text="Scale factor (images)", variable=self.size_mode, value="scale",
            command=self._sync_size_mode,
        ).grid(row=1, column=0, sticky="w", **pad)
        scale_row = ttk.Frame(opts)
        scale_row.grid(row=1, column=1, columnspan=5, sticky="w", **pad)
        self.scale = tk.StringVar(value="2")
        self.scale_entry = ttk.Entry(scale_row, textvariable=self.scale, width=7)
        self.scale_entry.pack(side="left")
        ttk.Label(
            scale_row,
            text="x  (per-image; videos in the queue still use the output size above)",
        ).pack(side="left", padx=6)

        ttk.Label(opts, text="Quality").grid(row=2, column=0, sticky="w", **pad)
        self.quality = tk.StringVar(value="ULTRA")
        ttk.Combobox(
            opts, textvariable=self.quality, values=QUALITIES, state="readonly", width=9
        ).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(opts, text="Codec").grid(row=2, column=2, sticky="w", **pad)
        self.codec = tk.StringVar(value="h264")
        ttk.Combobox(
            opts, textvariable=self.codec, values=CODECS, state="readonly", width=7
        ).grid(row=2, column=3, sticky="w", **pad)
        ttk.Label(opts, text="Preset").grid(row=2, column=4, sticky="w", **pad)
        self.preset = tk.StringVar(value="P7")
        ttk.Combobox(
            opts, textvariable=self.preset, values=PRESETS, state="readonly", width=5
        ).grid(row=2, column=5, sticky="w", **pad)

        ttk.Label(opts, text="Bitrate (Mbps)").grid(row=3, column=0, sticky="w", **pad)
        self.bitrate = tk.StringVar(value="16")
        ttk.Entry(opts, textvariable=self.bitrate, width=7).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(opts, text="Image format").grid(row=3, column=2, sticky="w", **pad)
        self.image_ext = tk.StringVar(value="(keep source)")
        ttk.Combobox(
            opts, textvariable=self.image_ext,
            values=("(keep source)", "png", "jpg", "webp"), state="readonly", width=12,
        ).grid(row=3, column=3, sticky="w", **pad)
        ttk.Label(opts, text="GPU").grid(row=3, column=4, sticky="w", **pad)
        self.device = tk.StringVar(value="0")
        ttk.Entry(opts, textvariable=self.device, width=5).grid(row=3, column=5, sticky="w", **pad)

        ttk.Label(opts, text="Output folder").grid(row=4, column=0, sticky="w", **pad)
        out_row = ttk.Frame(opts)
        out_row.grid(row=4, column=1, columnspan=5, sticky="ew", **pad)
        out_row.columnconfigure(0, weight=1)
        self.out_dir = tk.StringVar(value="")
        ttk.Entry(out_row, textvariable=self.out_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(out_row, text="Browse...", command=self._pick_out_dir).grid(row=0, column=1, padx=(6, 0))
        ttk.Label(
            opts, foreground="#666",
            text=f"Blank = <repo>/out/<source folder>/...   Worker: {resolve_python(None)}",
        ).grid(row=5, column=0, columnspan=6, sticky="w", **pad)

        # -- log
        logf = ttk.LabelFrame(root, text="Log")
        logf.grid(row=2, column=0, sticky="nsew", **pad)
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        self.log = tk.Text(logf, height=10, wrap="word", state="disabled", font=("Consolas", 9))
        self.log.grid(row=0, column=0, sticky="nsew", padx=(6, 0), pady=6)
        lscroll = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        lscroll.grid(row=0, column=1, sticky="ns", pady=6)
        self.log.configure(yscrollcommand=lscroll.set)

        # -- action bar
        bar = ttk.Frame(root)
        bar.grid(row=3, column=0, sticky="ew", **pad)
        bar.columnconfigure(1, weight=1)
        self.run_btn = ttk.Button(bar, text="Run", command=self._run)
        self.run_btn.grid(row=0, column=0)
        self.progress = ttk.Progressbar(bar, mode="determinate")
        self.progress.grid(row=0, column=1, sticky="ew", padx=8)
        self.status = ttk.Label(bar, text="Ready")
        self.status.grid(row=0, column=2, padx=(0, 8))
        self.open_btn = ttk.Button(bar, text="Open output", command=self._open_output, state="disabled")
        self.open_btn.grid(row=0, column=3)
        self._sync_size_mode()

    # -------------------------------------------------------------- helpers
    def _sync_size_mode(self) -> None:
        fixed = self.size_mode.get() == "fixed"
        for widget in (self.width_entry, self.height_entry):
            widget.configure(state="normal" if fixed else "disabled")
        self.size_preset.configure(state="readonly" if fixed else "disabled")
        self.scale_entry.configure(state="disabled" if fixed else "normal")

    def _apply_size_preset(self, _event=None) -> None:
        w, h = SIZE_PRESETS[self.size_preset.get()]
        self.width.set(str(w))
        self.height.set(str(h))

    def _append_paths(self, paths) -> None:
        for raw in paths:
            path = Path(raw)
            if path.suffix.lower() not in IMAGE_EXTS | VIDEO_EXTS:
                continue
            if path in self._files:
                continue
            self._files.append(path)
            kind = "img" if path.suffix.lower() in IMAGE_EXTS else "vid"
            self.listbox.insert("end", f"[{kind}] {path}")

    def _add_files(self) -> None:
        exts = " ".join(f"*{e}" for e in sorted(IMAGE_EXTS | VIDEO_EXTS))
        self._append_paths(
            filedialog.askopenfilenames(
                title="Add videos / images",
                filetypes=[
                    ("Videos and images", exts),
                    ("Videos", " ".join(f"*{e}" for e in sorted(VIDEO_EXTS))),
                    ("Images", " ".join(f"*{e}" for e in sorted(IMAGE_EXTS))),
                    ("All files", "*.*"),
                ],
            )
        )

    def _add_folder(self) -> None:
        folder = filedialog.askdirectory(title="Add every video/image in a folder")
        if folder:
            self._append_paths(sorted(p for p in Path(folder).iterdir() if p.is_file()))

    def _remove_selected(self) -> None:
        for index in sorted(self.listbox.curselection(), reverse=True):
            self.listbox.delete(index)
            del self._files[index]

    def _clear(self) -> None:
        self.listbox.delete(0, "end")
        self._files.clear()

    def _pick_out_dir(self) -> None:
        folder = filedialog.askdirectory(title="Output folder")
        if folder:
            self.out_dir.set(folder)

    def _log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip("\n") + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _open_output(self) -> None:
        if self._last_outputs:
            _open_folder(self._last_outputs[-1].parent)

    # ---------------------------------------------------------------- run
    def _build_argv(self) -> list[str]:
        if not self._files:
            raise ValueError("add at least one video or image")
        argv = [sys.executable, "-m", "fast_rtxvsr.cli", "run", *map(str, self._files)]
        w, h = int(self.width.get()), int(self.height.get())
        argv += ["--width", str(w), "--height", str(h)]
        if self.size_mode.get() == "scale":
            scale = float(self.scale.get())
            if scale <= 0:
                raise ValueError("scale must be > 0")
            argv += ["--scale", str(scale)]
        argv += [
            "--quality", self.quality.get(),
            "--codec", self.codec.get(),
            "--preset", self.preset.get(),
            "--bitrate", str(int(float(self.bitrate.get()) * 1_000_000)),
            "--device", str(int(self.device.get())),
        ]
        if self.image_ext.get() != "(keep source)":
            argv += ["--image-ext", self.image_ext.get()]
        if self.out_dir.get().strip():
            argv += ["--out-dir", self.out_dir.get().strip()]
        return argv

    def _run(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
            self._log("[gui] cancelled")
            return
        try:
            argv = self._build_argv()
        except ValueError as exc:
            messagebox.showerror("fast-rtxvsr", str(exc))
            return
        self._last_outputs = []
        self.open_btn.configure(state="disabled")
        self.progress.configure(value=0, maximum=max(1, len(self._files)))
        self.status.configure(text="Running...")
        self.run_btn.configure(text="Cancel")
        self._log("[gui] " + " ".join(argv[3:]))
        env = os.environ.copy()
        root = str(repo_root())
        env["PYTHONPATH"] = root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self._proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        for name, pipe in (("out", self._proc.stdout), ("err", self._proc.stderr)):
            threading.Thread(target=self._pump, args=(name, pipe), daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def _pump(self, name: str, pipe) -> None:
        for line in pipe:
            self._events.put((name, line))

    def _wait(self) -> None:
        code = self._proc.wait() if self._proc else -1
        self._events.put(("exit", str(code)))

    def _drain(self) -> None:
        try:
            while True:
                name, line = self._events.get_nowait()
                if name == "exit":
                    self._finish(int(line))
                elif name == "out":
                    self._on_event(line)
                else:
                    self._log(line)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _on_event(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            if line:
                self._log(line)
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self._log(line)
            return
        if event.get("event") == "done":
            if event.get("mode") == "image":
                for item in event.get("results") or []:
                    self._last_outputs.append(Path(item["output"]))
                self.progress.step(len(event.get("results") or []))
            else:
                self._last_outputs.append(Path(event["output"]))
                self.progress.step(1)
            self.status.configure(text=f"{len(self._last_outputs)} done")
        elif event.get("log") == "device":
            self._log(f"  {event.get('device')}  quality={event.get('quality')}  -> {event.get('output')}")
        elif event.get("error"):
            self._log(f"  error: {event['error']}")

    def _finish(self, code: int) -> None:
        self._proc = None
        self.run_btn.configure(text="Run")
        if code == 0:
            self.progress.configure(value=self.progress["maximum"])
            self.status.configure(text=f"Done: {len(self._last_outputs)} file(s)")
            self._log("[gui] done")
        else:
            self.status.configure(text=f"Failed (exit {code})")
            self._log(f"[gui] failed with exit code {code}")
        if self._last_outputs:
            self.open_btn.configure(state="normal")


def main() -> int:
    App().mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
