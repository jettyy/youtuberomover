#!/usr/bin/env python3
"""무음 자동컷 GUI - 더블클릭으로 실행하는 창 버전.

실제 처리는 silence_cut.py 의 기능을 그대로 사용한다.
tkinterdnd2 가 설치되어 있으면 파일/폴더 드래그&드롭도 지원한다.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import silence_cut as sc  # noqa: E402

try:  # 선택 사항: pip install tkinterdnd2
    from tkinterdnd2 import DND_FILES, TkinterDnD  # type: ignore
except Exception:
    TkinterDnD = None

APP_TITLE = "무음 자동컷"
SETTINGS_PATH = Path.home() / ".silence_cut_gui.json"

PRESETS = {
    "자연스럽게": {"min_silence": 0.8, "padding": 0.20},
    "기본": {"min_silence": 0.5, "padding": 0.12},
    "빡빡하게": {"min_silence": 0.35, "padding": 0.08},
}
CUSTOM = "직접 설정"
ENCODERS = {
    "자동 (GPU 있으면 GPU 사용, 빠름)": "auto",
    "CPU (느리지만 어디서나 동작)": "cpu",
}
DEFAULTS = {
    "preset": "기본", "db": -30.0, "min_silence": 0.5, "padding": 0.12, "max_silence": 10.0,
    "encoder": "auto", "out_mode": "same", "out_dir": "", "overwrite": False, "open_after": True,
}


class QueueWriter:
    """print() 출력을 GUI 로그로 보내기 위한 stdout 대체 객체."""

    def __init__(self, q: "queue.Queue"):
        self.q = q

    def write(self, s: str) -> int:
        if s:
            self.q.put(("log", s))
        return len(s)

    def flush(self) -> None:
        pass


def short_time(seconds: float) -> str:
    """초 -> 1:02:03 또는 2:03"""
    t = int(round(seconds))
    h, m, sec = t // 3600, t % 3600 // 60, t % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def open_folder(path: Path) -> None:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception:
        pass


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.q: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.jobs: List[Tuple[Path, Path]] = []
        self.current_index = 0
        self.last_out_dir: Optional[Path] = None
        self._closing = False

        root.title(APP_TITLE)
        root.geometry("900x760")
        root.minsize(760, 620)

        s = self.load_settings()
        self.preset_var = tk.StringVar(value=s["preset"])
        self.db_var = tk.StringVar(value=f"{s['db']:g}")
        self.min_var = tk.StringVar(value=f"{s['min_silence']:g}")
        self.pad_var = tk.StringVar(value=f"{s['padding']:g}")
        self.max_var = tk.StringVar(value=f"{s['max_silence']:g}")
        enc_label = next((k for k, v in ENCODERS.items() if v == s["encoder"]), next(iter(ENCODERS)))
        self.enc_var = tk.StringVar(value=enc_label)
        self.out_mode = tk.StringVar(value=s["out_mode"])
        self.out_dir = tk.StringVar(value=s["out_dir"])
        self.overwrite_var = tk.BooleanVar(value=s["overwrite"])
        self.open_after_var = tk.BooleanVar(value=s["open_after"])
        self.status_var = tk.StringVar(value="mp4 파일을 추가한 뒤 [컷 시작]을 누르세요.")

        self._build()
        for var in (self.min_var, self.pad_var):
            var.trace_add("write", lambda *_: self.sync_preset_label())
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_queue)

    # ------------------------------------------------------------------ UI
    def _build(self) -> None:
        style = ttk.Style()
        style.configure("Accent.TButton", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11, "bold"),
                        padding=(16, 6))
        style.configure("Hint.TLabel", foreground="#6b6b6b")

        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(0, weight=1)

        # 1) 파일 목록 ------------------------------------------------------
        files = ttk.LabelFrame(main, text=" 1. 영상 파일 ", padding=8)
        files.grid(row=0, column=0, sticky="nsew")
        files.columnconfigure(0, weight=1)
        files.rowconfigure(1, weight=1)
        main.rowconfigure(0, weight=2)

        bar = ttk.Frame(files)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.btn_add = ttk.Button(bar, text="파일 추가", command=self.add_files)
        self.btn_add_dir = ttk.Button(bar, text="폴더 추가", command=self.add_folder)
        self.btn_remove = ttk.Button(bar, text="선택 제거", command=self.remove_selected)
        self.btn_clear = ttk.Button(bar, text="전체 비우기", command=self.clear_files)
        for b in (self.btn_add, self.btn_add_dir, self.btn_remove, self.btn_clear):
            b.pack(side="left", padx=(0, 6))
        hint = "여기로 파일/폴더를 끌어다 놓아도 됩니다" if TkinterDnD else ""
        ttk.Label(bar, text=hint, style="Hint.TLabel").pack(side="right")

        tree_wrap = ttk.Frame(files)
        tree_wrap.grid(row=1, column=0, sticky="nsew")
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(tree_wrap, columns=("name", "status", "folder"), show="headings", height=6)
        self.tree.heading("name", text="파일")
        self.tree.heading("status", text="상태")
        self.tree.heading("folder", text="위치")
        self.tree.column("name", width=240)
        self.tree.column("status", width=330)
        self.tree.column("folder", width=240)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ysb = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        ysb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=ysb.set)
        self.tree.bind("<Delete>", lambda e: self.remove_selected())
        if TkinterDnD:
            for w in (self.tree, files):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        # 2) 설정 ------------------------------------------------------------
        opts = ttk.LabelFrame(main, text=" 2. 설정 ", padding=8)
        opts.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        opts.columnconfigure(2, weight=1)

        r = 0
        ttk.Label(opts, text="컷 강도").grid(row=r, column=0, sticky="w", pady=3)
        self.preset_cb = ttk.Combobox(opts, textvariable=self.preset_var, state="readonly", width=14,
                                      values=list(PRESETS) + [CUSTOM])
        self.preset_cb.grid(row=r, column=1, sticky="w", padx=8)
        self.preset_cb.bind("<<ComboboxSelected>>", lambda e: self.apply_preset())
        ttk.Label(opts, text="자연스럽게 = 짧은 쉼은 남김 / 빡빡하게 = 최대한 촘촘히 자름",
                  style="Hint.TLabel").grid(row=r, column=2, sticky="w")

        fields = [
            ("무음 기준 (dB)", self.db_var, -70, -10, 1, "이 소리보다 작으면 무음. 배경 소음이 크면 -25 쪽으로 올리기"),
            ("최소 무음 길이 (초)", self.min_var, 0.1, 5, 0.05, "이보다 짧은 쉼은 자르지 않음"),
            ("앞뒤 여유 (초)", self.pad_var, 0, 1, 0.01, "말 앞뒤로 남겨둘 여유. 말끝이 잘리면 늘리기"),
            ("긴 무음 보호 (초)", self.max_var, 0, 600, 1, "이보다 긴 무음은 의도적일 수 있어 자르지 않고 알려줌 (0 = 끔)"),
        ]
        for label, var, lo, hi, step, desc in fields:
            r += 1
            ttk.Label(opts, text=label).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Spinbox(opts, textvariable=var, from_=lo, to=hi, increment=step, width=12).grid(
                row=r, column=1, sticky="w", padx=8)
            ttk.Label(opts, text=desc, style="Hint.TLabel").grid(row=r, column=2, sticky="w")

        r += 1
        ttk.Label(opts, text="인코딩").grid(row=r, column=0, sticky="w", pady=3)
        ttk.Combobox(opts, textvariable=self.enc_var, state="readonly", width=32,
                     values=list(ENCODERS)).grid(row=r, column=1, columnspan=2, sticky="w", padx=8)

        r += 1
        ttk.Label(opts, text="저장 위치").grid(row=r, column=0, sticky="nw", pady=3)
        outf = ttk.Frame(opts)
        outf.grid(row=r, column=1, columnspan=2, sticky="ew", padx=8)
        outf.columnconfigure(2, weight=1)
        ttk.Radiobutton(outf, text="원본과 같은 폴더", value="same", variable=self.out_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(outf, text="다른 폴더:", value="custom", variable=self.out_mode).grid(row=0, column=1, sticky="w", padx=(12, 4))
        ttk.Entry(outf, textvariable=self.out_dir).grid(row=0, column=2, sticky="ew")
        ttk.Button(outf, text="찾아보기", command=self.pick_out_dir).grid(row=0, column=3, padx=(4, 0))

        r += 1
        chk = ttk.Frame(opts)
        chk.grid(row=r, column=1, columnspan=2, sticky="w", padx=8, pady=(4, 0))
        ttk.Checkbutton(chk, text="이미 있는 _cut 파일 덮어쓰기", variable=self.overwrite_var).pack(side="left")
        ttk.Checkbutton(chk, text="끝나면 폴더 열기", variable=self.open_after_var).pack(side="left", padx=(16, 0))

        # 3) 실행 ------------------------------------------------------------
        run = ttk.LabelFrame(main, text=" 3. 실행 ", padding=8)
        run.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        run.columnconfigure(0, weight=1)
        run.rowconfigure(4, weight=1)
        main.rowconfigure(2, weight=3)

        btns = ttk.Frame(run)
        btns.grid(row=0, column=0, sticky="ew")
        self.btn_start = ttk.Button(btns, text="▶  컷 시작", style="Accent.TButton", command=lambda: self.start(False))
        self.btn_preview = ttk.Button(btns, text="미리 분석 (자르지 않고 결과만 보기)", command=lambda: self.start(True))
        self.btn_stop = ttk.Button(btns, text="■  중지", command=self.stop, state="disabled")
        self.btn_open = ttk.Button(btns, text="결과 폴더 열기", command=self.open_result_folder)
        self.btn_start.pack(side="left")
        self.btn_preview.pack(side="left", padx=6)
        self.btn_stop.pack(side="left")
        self.btn_open.pack(side="right")

        ttk.Label(run, textvariable=self.status_var).grid(row=1, column=0, sticky="w", pady=(8, 2))
        bars = ttk.Frame(run)
        bars.grid(row=2, column=0, rowspan=2, sticky="ew", pady=(0, 8))
        bars.columnconfigure(1, weight=1)
        ttk.Label(bars, text="현재 단계").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.pb_file = ttk.Progressbar(bars, maximum=100)
        self.pb_file.grid(row=0, column=1, sticky="ew")
        ttk.Label(bars, text="전체").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(4, 0))
        self.pb_total = ttk.Progressbar(bars, maximum=100)
        self.pb_total.grid(row=1, column=1, sticky="ew", pady=(4, 0))

        logf = ttk.Frame(run)
        logf.grid(row=4, column=0, sticky="nsew")
        logf.columnconfigure(0, weight=1)
        logf.rowconfigure(0, weight=1)
        mono = "Consolas" if os.name == "nt" else "TkFixedFont"
        self.log = tk.Text(logf, height=10, wrap="none", state="disabled", font=(mono, 9),
                           relief="flat", background="#f7f7f7")
        self.log.grid(row=0, column=0, sticky="nsew")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        lsb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=lsb.set)
        self.log.tag_configure("warn", foreground="#b35c00")
        self.log.tag_configure("err", foreground="#c62828")
        self.log.tag_configure("head", font=(mono, 9, "bold"))

        self.set_running(False)

    # ------------------------------------------------------------ 설정 값
    def load_settings(self) -> dict:
        s = dict(DEFAULTS)
        try:
            s.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
        return s

    def save_settings(self) -> None:
        try:
            p = self.read_params(silent=True) or {}
            data = {"preset": self.preset_var.get(), "encoder": ENCODERS.get(self.enc_var.get(), "auto"),
                    "out_mode": self.out_mode.get(), "out_dir": self.out_dir.get(),
                    "overwrite": self.overwrite_var.get(), "open_after": self.open_after_var.get(), **p}
            SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def apply_preset(self) -> None:
        p = PRESETS.get(self.preset_var.get())
        if p:
            self.min_var.set(f"{p['min_silence']:g}")
            self.pad_var.set(f"{p['padding']:g}")

    def sync_preset_label(self) -> None:
        try:
            cur = (float(self.min_var.get()), float(self.pad_var.get()))
        except ValueError:
            self.preset_var.set(CUSTOM)
            return
        for name, p in PRESETS.items():
            if abs(cur[0] - p["min_silence"]) < 1e-9 and abs(cur[1] - p["padding"]) < 1e-9:
                self.preset_var.set(name)
                return
        self.preset_var.set(CUSTOM)

    def read_params(self, silent: bool = False) -> Optional[dict]:
        checks = [("무음 기준", self.db_var, -120, 0), ("최소 무음 길이", self.min_var, 0.05, 60),
                  ("앞뒤 여유", self.pad_var, 0, 5), ("긴 무음 보호", self.max_var, 0, 36000)]
        vals = []
        for name, var, lo, hi in checks:
            try:
                v = float(var.get())
                if not lo <= v <= hi:
                    raise ValueError
            except ValueError:
                if not silent:
                    messagebox.showerror(APP_TITLE, f"'{name}' 값이 올바르지 않습니다 ({lo} ~ {hi}).")
                return None
            vals.append(v)
        return {"db": vals[0], "min_silence": vals[1], "padding": vals[2], "max_silence": vals[3]}

    # ------------------------------------------------------------ 파일 목록
    def add_paths(self, paths) -> None:
        added = 0
        for p in paths:
            p = Path(p)
            if p.is_dir():
                cands = sorted(x for x in p.iterdir() if x.is_file() and x.suffix.lower() == ".mp4"
                               and not x.stem.endswith("_cut"))
            elif p.is_file() and p.suffix.lower() == ".mp4":
                cands = [p]
            else:
                cands = []
            for f in cands:
                iid = str(f.resolve())
                if not self.tree.exists(iid):
                    self.tree.insert("", "end", iid=iid, values=(f.name, "대기", str(f.parent)))
                    added += 1
        if added == 0 and paths:
            self.status_var.set("추가할 새 mp4 파일이 없습니다.")
        elif added:
            self.status_var.set(f"{added}개 파일을 추가했습니다. 총 {len(self.tree.get_children())}개")

    def add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="영상 파일 선택",
                                            filetypes=[("MP4 영상", "*.mp4 *.MP4"), ("모든 파일", "*.*")])
        self.add_paths(paths)

    def add_folder(self) -> None:
        d = filedialog.askdirectory(title="영상 폴더 선택")
        if d:
            self.add_paths([d])

    def on_drop(self, event) -> None:
        self.add_paths(self.root.tk.splitlist(event.data))

    def remove_selected(self) -> None:
        if not self.running:
            for iid in self.tree.selection():
                self.tree.delete(iid)

    def clear_files(self) -> None:
        if not self.running:
            self.tree.delete(*self.tree.get_children())

    def pick_out_dir(self) -> None:
        d = filedialog.askdirectory(title="저장할 폴더 선택")
        if d:
            self.out_dir.set(d)
            self.out_mode.set("custom")

    # ------------------------------------------------------------ 실행
    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def set_running(self, running: bool) -> None:
        normal = "disabled" if running else "normal"
        for b in (self.btn_start, self.btn_preview, self.btn_add, self.btn_add_dir,
                  self.btn_remove, self.btn_clear):
            b.configure(state=normal)
        self.btn_stop.configure(state="normal" if running else "disabled")

    def start(self, dry: bool) -> None:
        items = list(self.tree.get_children())
        if not items:
            messagebox.showinfo(APP_TITLE, "먼저 [파일 추가] 또는 [폴더 추가]로 영상을 넣어 주세요.")
            return
        params = self.read_params()
        if params is None:
            return
        out_dir: Optional[Path] = None
        if self.out_mode.get() == "custom":
            if not self.out_dir.get().strip():
                messagebox.showerror(APP_TITLE, "저장할 폴더를 선택해 주세요.")
                return
            out_dir = Path(self.out_dir.get().strip())
        self.jobs = []
        for iid in items:
            src = Path(iid)
            out = (out_dir or src.parent) / f"{src.stem}_cut.mp4"
            self.jobs.append((src, out))
            self.tree.set(iid, "status", "대기")
        self.last_out_dir = self.jobs[0][1].parent
        self.save_settings()
        self.clear_log()
        self.pb_file["value"] = 0
        self.pb_total["value"] = 0
        encoder = ENCODERS.get(self.enc_var.get(), "auto")
        opts = dict(params, encoder=encoder, overwrite=self.overwrite_var.get(), dry=dry)
        self.worker = threading.Thread(target=self.run_jobs, args=(list(self.jobs), opts), daemon=True)
        self.set_running(True)
        self.status_var.set("준비 중...")
        self.worker.start()

    def stop(self) -> None:
        sc.request_cancel()
        self.btn_stop.configure(state="disabled")
        self.status_var.set("중지하는 중... (임시 파일 정리)")

    def run_jobs(self, jobs: List[Tuple[Path, Path]], o: dict) -> None:
        """작업 스레드. GUI 위젯은 건드리지 않고 큐로만 알린다."""
        q = self.q
        sc.reset_cancel()
        sc.progress_hook = lambda label, pct, speed: q.put(("progress", label, pct, speed))
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout = sys.stderr = QueueWriter(q)  # type: ignore[assignment]
        done = failed = 0
        cancelled = False
        try:
            try:
                tools = sc.find_tools(None)
            except sc.SilenceCutError as e:
                q.put(("fatal", str(e)))
                return
            for i, (src, out) in enumerate(jobs):
                q.put(("file_start", i, src))
                argv = ["--input", str(src), "--output", str(out), "--db", str(o["db"]),
                        "--min-silence", str(o["min_silence"]), "--padding", str(o["padding"]),
                        "--max-silence", str(o["max_silence"]), "--encoder", o["encoder"]]
                if o["overwrite"]:
                    argv.append("--overwrite")
                if o["dry"]:
                    argv.append("--dry-run")
                args = sc.build_parser().parse_args(argv)
                try:
                    sc.check_cancel()
                    r = sc.process_file(tools, src, out, args)
                except sc.Cancelled:
                    cancelled = True
                    q.put(("file_done", i, "중지됨"))
                    break
                except sc.SilenceCutError as e:
                    print(f"  [오류] {e}")
                    failed += 1
                    q.put(("file_done", i, "실패 (로그 확인)"))
                    continue
                except Exception:
                    print("  [오류] 예기치 못한 문제:\n" + traceback.format_exc())
                    failed += 1
                    q.put(("file_done", i, "실패 (로그 확인)"))
                    continue
                if r is None:
                    q.put(("file_done", i, "건너뜀 (로그 확인)"))
                    continue
                done += 1
                pct = (1 - r["final"] / r["orig"]) * 100 if r["orig"] else 0
                span = f"{short_time(r['orig'])} → {short_time(r['final'])}"
                word = "분석" if r.get("dry_run") else "완료"
                q.put(("file_done", i, f"{word} · 컷 {r['cuts']}개 · {span} (-{pct:.0f}%)"))
        finally:
            sys.stdout, sys.stderr = old_out, old_err
            sc.progress_hook = None
            q.put(("finished", done, failed, cancelled, o["dry"]))

    # ------------------------------------------------------------ 큐 처리
    def poll_queue(self) -> None:
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        if not self._closing:
            self.root.after(100, self.poll_queue)

    def handle(self, msg) -> None:
        kind = msg[0]
        n = max(1, len(self.jobs))
        if kind == "log":
            self.append_log(msg[1])
        elif kind == "file_start":
            self.current_index = msg[1]
            src: Path = msg[2]
            self.tree.set(str(src.resolve()), "status", "처리 중...")
            self.tree.see(str(src.resolve()))
            self.pb_file["value"] = 0
            self.pb_total["value"] = self.current_index / n * 100
            self.status_var.set(f"[{self.current_index + 1}/{n}] {src.name}")
        elif kind == "progress":
            _, label, pct, speed = msg
            self.pb_file["value"] = pct
            frac = self.phase_fraction(label, pct)
            self.pb_total["value"] = (self.current_index + frac) / n * 100
            name = self.jobs[self.current_index][0].name if self.jobs else ""
            sp = f"  ·  {speed}" if speed else ""
            self.status_var.set(f"[{self.current_index + 1}/{n}] {name}  —  {label} {pct:.0f}%{sp}")
        elif kind == "file_done":
            _, i, text = msg
            src = self.jobs[i][0]
            self.tree.set(str(src.resolve()), "status", text)
        elif kind == "fatal":
            self.append_log(msg[1] + "\n", "err")
            messagebox.showerror(APP_TITLE, msg[1])
        elif kind == "finished":
            _, done, failed, cancelled, dry = msg
            self.set_running(False)
            self.pb_file["value"] = 100 if not cancelled else self.pb_file["value"]
            if not cancelled:
                self.pb_total["value"] = 100
            word = "분석" if dry else "처리"
            summary = f"{word} 완료: 성공 {done}개" + (f", 실패 {failed}개" if failed else "")
            if cancelled:
                summary = "중지되었습니다. " + summary
            self.status_var.set(summary)
            if self._closing:
                return  # poll_until_closed 가 창을 닫는다
            if not cancelled and not dry and done and self.open_after_var.get() and self.last_out_dir:
                open_folder(self.last_out_dir)

    @staticmethod
    def phase_fraction(label: str, pct: float) -> float:
        """현재 단계(탐지/렌더링/합치기)를 파일 전체 진행률(0~1)로 환산."""
        p = pct / 100
        if label.startswith("무음"):
            return 0.1 * p
        if label.startswith("합치기"):
            return 0.95 + 0.05 * p
        if label.startswith("렌더링"):
            k, total = 1, 1
            parts = label.split()
            if len(parts) > 1 and "/" in parts[1]:
                try:
                    k, total = (int(x) for x in parts[1].split("/"))
                except ValueError:
                    pass
            return 0.1 + 0.85 * ((k - 1) + p) / total
        return 0.0

    # ------------------------------------------------------------ 로그
    def append_log(self, text: str, tag: Optional[str] = None) -> None:
        if tag is None:
            if "[오류]" in text:
                tag = "err"
            elif "[경고]" in text:
                tag = "warn"
            elif text.startswith("\n[") or text.startswith("["):
                tag = "head"
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def open_result_folder(self) -> None:
        if self.out_mode.get() == "custom" and self.out_dir.get().strip():
            open_folder(Path(self.out_dir.get().strip()))
        elif self.last_out_dir:
            open_folder(self.last_out_dir)
        else:
            items = self.tree.get_children()
            if items:
                open_folder(Path(items[0]).parent)

    def on_close(self) -> None:
        self.save_settings()
        if self.running:
            if not messagebox.askyesno(APP_TITLE, "작업이 진행 중입니다. 중지하고 종료할까요?"):
                return
            self._closing = True
            sc.request_cancel()
            self.status_var.set("중지하는 중... 잠시 후 종료됩니다.")
            self.root.after(100, self.poll_until_closed)
            return
        self.root.destroy()

    def poll_until_closed(self) -> None:
        try:
            while True:
                self.handle(self.q.get_nowait())
        except queue.Empty:
            pass
        except tk.TclError:
            return
        if self.worker is not None and self.worker.is_alive():
            self.root.after(100, self.poll_until_closed)
        else:
            try:
                self.root.destroy()
            except tk.TclError:
                pass


def main() -> None:
    if os.name == "nt":
        try:  # 고해상도 모니터에서 글자가 흐릿하지 않게
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    if os.name == "nt":
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            try:
                tkfont.nametofont(name).configure(family="맑은 고딕", size=10)
            except tk.TclError:
                pass
    try:
        App(root)
    except Exception:
        messagebox.showerror(APP_TITLE, traceback.format_exc())
        raise
    root.mainloop()


if __name__ == "__main__":
    main()
