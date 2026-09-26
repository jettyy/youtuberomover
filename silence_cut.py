#!/usr/bin/env python3
"""silence_cut.py - 롱폼 영상의 무음(pause) 구간을 자동으로 잘라내는 CLI.

처리 흐름:
  1) ffprobe 로 원본 정보 확인
  2) ffmpeg silencedetect 로 무음 구간 탐지
  3) 패딩/최소/최대 길이 규칙을 적용해 남길 구간(EDL) 생성 (프레임 그리드에 스냅)
  4) 구간을 청크 단위로 정확히 잘라 렌더링 (비디오 1회 인코딩, 오디오는 청크 단계 PCM)
  5) 청크를 concat demuxer 로 이어붙이며 오디오를 AAC 로 1회 인코딩

외부 파이썬 패키지 없이 표준 라이브러리 + ffmpeg/ffprobe 만 사용한다.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# 오디오 컷 경계의 클릭/팝 노이즈 방지용 페이드 길이(초)
AUDIO_FADE = 0.008
# 청크 렌더링 시 첫 구간 앞쪽으로 여유 있게 seek 하는 거리(초)
SEEK_MARGIN = 1.0
# VFR 소스를 CFR 로 맞출 때 사용하는 표준 프레임레이트
STANDARD_FPS = [
    Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001),
    Fraction(30), Fraction(48), Fraction(50), Fraction(60000, 1001), Fraction(60),
    Fraction(90), Fraction(100), Fraction(120), Fraction(144), Fraction(165),
    Fraction(240),
]

FFMPEG_INSTALL_GUIDE = """\
ffmpeg / ffprobe 를 찾을 수 없습니다.

  Windows 설치 방법 (택 1):
    - winget install Gyan.FFmpeg
    - choco install ffmpeg
    - https://www.gyan.dev/ffmpeg/builds/ 에서 'full' 빌드를 받아 압축을 풀고
      bin 폴더를 PATH 환경변수에 추가
  설치 후 새 터미널에서 'ffmpeg -version' 이 동작하는지 확인하세요.
  PATH 에 넣지 않았다면 --ffmpeg "C:\\ffmpeg\\bin\\ffmpeg.exe" 로 직접 지정할 수 있습니다.
"""


class SilenceCutError(Exception):
    pass


class Cancelled(SilenceCutError):
    pass


# Windows 에서 ffmpeg 실행 시 콘솔 창이 깜빡이지 않도록 (GUI 에서 실행할 때 필요)
POPEN_KW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}

# GUI 연동: 진행률 콜백 hook(label, percent, speed) 과 중지 요청
progress_hook = None
_cancel_requested = False
_current_proc: Optional[subprocess.Popen] = None


def request_cancel() -> None:
    """실행 중인 작업을 중지한다 (다른 스레드에서 호출 가능)."""
    global _cancel_requested
    _cancel_requested = True
    proc = _current_proc
    if proc is not None and proc.poll() is None:
        proc.kill()


def reset_cancel() -> None:
    global _cancel_requested
    _cancel_requested = False


def check_cancel() -> None:
    if _cancel_requested:
        raise Cancelled("사용자가 중지했습니다.")


# ---------------------------------------------------------------------------
# 공용 유틸
# ---------------------------------------------------------------------------

def tc(seconds: float) -> str:
    """초 -> HH:MM:SS.s"""
    seconds = max(0.0, seconds)
    h = int(seconds // 3600)
    m = int(seconds % 3600 // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:04.1f}"


def fps_str(fps: Fraction) -> str:
    return str(fps.numerator) if fps.denominator == 1 else f"{fps.numerator}/{fps.denominator}"


def parse_fraction(value: Optional[str]) -> Optional[Fraction]:
    try:
        f = Fraction(value)  # "30000/1001", "30/1"
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def to_int(value) -> Optional[int]:
    try:
        v = int(float(value))
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


@dataclass
class Tools:
    ffmpeg: str
    ffprobe: str
    version: Optional[Tuple[int, int]]  # None = 버전 문자열 해석 불가(최신 git 빌드로 간주)

    def newer_than(self, major: int, minor: int = 0) -> bool:
        return self.version is None or self.version >= (major, minor)

    def filter_script_opt(self) -> str:
        # ffmpeg 7.0 부터 -/filter_complex <파일> 문법, 이전은 -filter_complex_script
        return "-/filter_complex" if self.newer_than(7, 0) else "-filter_complex_script"

    def cfr_opts(self) -> List[str]:
        return ["-fps_mode", "cfr"] if self.newer_than(5, 1) else ["-vsync", "cfr"]


def find_tools(ffmpeg_arg: Optional[str]) -> Tools:
    if ffmpeg_arg:
        ffmpeg = shutil.which(ffmpeg_arg) or (ffmpeg_arg if Path(ffmpeg_arg).is_file() else None)
        ffprobe = None
        if ffmpeg:
            p = Path(ffmpeg)
            cand = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
            ffprobe = str(cand) if cand.is_file() else shutil.which("ffprobe")
    else:
        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise SilenceCutError(FFMPEG_INSTALL_GUIDE)
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-version"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", check=True, **POPEN_KW).stdout
    except (OSError, subprocess.CalledProcessError):
        raise SilenceCutError(FFMPEG_INSTALL_GUIDE)
    first = out.splitlines()[0] if out else ""
    m = re.search(r"ffmpeg version n?(\d+)\.(\d+)", first)
    version = (int(m.group(1)), int(m.group(2))) if m else None
    if version and version < (4, 3):
        print(f"[경고] ffmpeg {version[0]}.{version[1]} 은 오래된 버전입니다. 5.1 이상을 권장합니다.")
    print(f"ffmpeg: {first.replace('ffmpeg version ', '').split(' Copyright')[0]}")
    return Tools(ffmpeg, ffprobe, version)


def run_ffmpeg(cmd: Sequence[str], log_path: Path, total: float, label: str) -> None:
    """ffmpeg 를 실행하며 -progress 출력으로 진행률을 표시한다.

    stderr 는 파이프 교착을 피하려고 로그 파일로 보낸다.
    """
    global _current_proc
    check_cancel()
    cmd = list(cmd)
    # -progress 는 출력 옵션이 아니라 전역 옵션이므로 앞쪽에 삽입
    cmd[1:1] = ["-nostdin", "-nostats", "-progress", "pipe:1"]
    hook = progress_hook
    last_print = 0.0
    out_time = 0.0
    speed = ""
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=log,
                                text=True, encoding="utf-8", errors="replace", **POPEN_KW)
        _current_proc = proc
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                key, _, val = line.strip().partition("=")
                if key in ("out_time_us", "out_time_ms"):  # 둘 다 실제로는 마이크로초
                    t = to_int(val)
                    if t:
                        out_time = t / 1_000_000
                elif key == "speed":
                    speed = val.strip()
                elif key == "progress":
                    now = time.monotonic()
                    if val == "end" or now - last_print > 0.5:
                        last_print = now
                        pct = min(100.0, out_time / total * 100) if total > 0 else 0.0
                        sp = speed if speed and speed != "N/A" else ""
                        if hook:
                            hook(label, pct, sp)
                        else:
                            sys.stdout.write(f"\r  {label}: {pct:5.1f}%  ({tc(out_time)} / {tc(total)})  {sp}   ")
                            sys.stdout.flush()
            proc.wait()
        except BaseException:
            proc.kill()
            proc.wait()
            if not hook:
                sys.stdout.write("\n")
            raise
        finally:
            _current_proc = None
    if not hook:
        sys.stdout.write("\n")
    check_cancel()
    if proc.returncode != 0:
        tail = log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-15:]
        raise SilenceCutError(f"ffmpeg 실행 실패 ({label}, 코드 {proc.returncode}):\n    "
                              + "\n    ".join(tail))


# ---------------------------------------------------------------------------
# 1) 원본 정보
# ---------------------------------------------------------------------------

@dataclass
class MediaInfo:
    path: Path
    duration: float
    fps: Fraction
    vfr: bool
    width: int
    height: int
    vcodec: str
    pix_fmt: str
    vbitrate: Optional[int]
    color: Dict[str, str]
    audio: List[dict]

    @property
    def total_frames(self) -> int:
        return int(round(self.duration * self.fps))


def pick_fps(avg: Optional[Fraction], rfr: Optional[Fraction]) -> Tuple[Fraction, bool]:
    if avg and rfr and abs(avg - rfr) / rfr < Fraction(1, 100):
        return rfr, False
    base = avg or rfr
    if base is None:
        raise SilenceCutError("프레임레이트를 알 수 없습니다. --fps 로 지정하세요.")
    # VFR: r_frame_rate 가 표준값이면 그것을, 아니면 평균에 가장 가까운 표준값을 사용
    if rfr and any(abs(rfr - s) / s < Fraction(1, 200) for s in STANDARD_FPS):
        return rfr, True
    return min(STANDARD_FPS, key=lambda s: abs(s - base)), True


def probe(tools: Tools, path: Path, track: int, fps_override: Optional[Fraction]) -> MediaInfo:
    res = subprocess.run(
        [tools.ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", **POPEN_KW)
    if res.returncode != 0:
        raise SilenceCutError(f"ffprobe 실패: {res.stderr.strip()}")
    data = json.loads(res.stdout or "{}")
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if video is None:
        raise SilenceCutError("비디오 스트림이 없습니다.")
    if not audio:
        raise SilenceCutError("오디오 스트림이 없습니다 (무음 탐지 불가).")
    if track >= len(audio):
        raise SilenceCutError(f"오디오 트랙 {track} 이 없습니다 (트랙 수: {len(audio)}).")

    duration = float(video.get("duration") or fmt.get("duration") or 0)
    if duration <= 0:
        raise SilenceCutError("영상 길이를 알 수 없습니다.")

    if fps_override:
        fps, vfr = fps_override, False
    else:
        fps, vfr = pick_fps(parse_fraction(video.get("avg_frame_rate")),
                            parse_fraction(video.get("r_frame_rate")))

    vbitrate = to_int(video.get("bit_rate"))
    if vbitrate is None:
        total = to_int(fmt.get("bit_rate"))
        abr = sum(to_int(a.get("bit_rate")) or 0 for a in audio)
        if total and total > abr:
            vbitrate = total - abr

    color = {k: video[k] for k in ("color_range", "color_space", "color_transfer", "color_primaries")
             if video.get(k) and video[k] not in ("unknown", "reserved")}

    return MediaInfo(
        path=path, duration=duration, fps=fps, vfr=vfr,
        width=int(video.get("width", 0)), height=int(video.get("height", 0)),
        vcodec=video.get("codec_name", ""), pix_fmt=video.get("pix_fmt", "yuv420p"),
        vbitrate=vbitrate, color=color, audio=audio,
    )


# ---------------------------------------------------------------------------
# 2) 무음 탐지
# ---------------------------------------------------------------------------

RE_START = re.compile(r"silence_start:\s*(-?[\d.]+)")
RE_END = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detect_silences(tools: Tools, info: MediaInfo, db: float, min_silence: float,
                    track: int, workdir: Path) -> List[Tuple[float, float]]:
    log = workdir / "detect.log"
    cmd = [tools.ffmpeg, "-hide_banner", "-v", "info", "-i", str(info.path),
           "-map", f"0:a:{track}", "-af", f"silencedetect=noise={db}dB:d={min_silence}",
           "-f", "null", "-"]
    run_ffmpeg(cmd, log, info.duration, "무음 탐지")
    silences: List[Tuple[float, float]] = []
    start: Optional[float] = None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        m = RE_START.search(line)
        if m:
            start = max(0.0, float(m.group(1)))
            continue
        m = RE_END.search(line)
        if m and start is not None:
            silences.append((start, min(float(m.group(1)), info.duration)))
            start = None
    if start is not None:  # 무음으로 끝나는 영상
        silences.append((start, info.duration))
    return silences


# ---------------------------------------------------------------------------
# 3) 컷 리스트(EDL)
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    fps: Fraction
    total_frames: int
    silences: List[Tuple[float, float]]
    keeps: List[Tuple[int, int]] = field(default_factory=list)   # 프레임 단위 [start, end)
    cuts: List[Tuple[int, int]] = field(default_factory=list)
    skipped_long: List[Tuple[float, float]] = field(default_factory=list)

    def sec(self, frame: int) -> float:
        return float(frame / self.fps)

    @property
    def kept_frames(self) -> int:
        return sum(b - a for a, b in self.keeps)

    @property
    def removed_seconds(self) -> float:
        return self.sec(self.total_frames - self.kept_frames)

    @property
    def final_seconds(self) -> float:
        return self.sec(self.kept_frames)


def complement(ranges: List[Tuple[int, int]], total: int) -> List[Tuple[int, int]]:
    out, pos = [], 0
    for a, b in ranges:
        if a > pos:
            out.append((pos, a))
        pos = max(pos, b)
    if pos < total:
        out.append((pos, total))
    return out


def build_plan(silences: List[Tuple[float, float]], info: MediaInfo, padding: float,
               max_silence: float, min_keep: float) -> Plan:
    fps, total = info.fps, info.total_frames
    plan = Plan(fps=fps, total_frames=total, silences=silences)

    raw: List[Tuple[int, int]] = []
    for s, e in silences:
        if max_silence > 0 and e - s >= max_silence:
            plan.skipped_long.append((s, e))
            continue
        cs, ce = s + padding, e - padding
        if ce <= cs:
            continue
        # 프레임 그리드에 스냅 -> 오디오/비디오 컷 길이가 정확히 일치해 싱크 누적 오차가 없다
        fa = max(0, int(round(cs * fps)))
        fb = min(total, int(round(ce * fps)))
        if fb > fa:
            raw.append((fa, fb))

    # 겹치거나 맞닿은 컷 병합
    raw.sort()
    merged: List[Tuple[int, int]] = []
    for a, b in raw:
        if merged and a <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))

    # 너무 짧게 남는 조각(몇 프레임 번쩍임)은 컷에 흡수
    min_keep_frames = max(1, int(round(min_keep * fps)))
    keeps = [k for k in complement(merged, total) if k[1] - k[0] >= min_keep_frames]
    plan.keeps = keeps
    plan.cuts = complement(keeps, total)
    return plan


def save_edl(plan: Plan, info: MediaInfo, args: argparse.Namespace, path: Path) -> None:
    data = {
        "source": str(info.path),
        "fps": fps_str(plan.fps),
        "params": {"db": args.db, "min_silence": args.min_silence, "padding": args.padding,
                   "max_silence": args.max_silence, "min_keep": args.min_keep,
                   "audio_track": args.audio_track},
        "original_seconds": round(plan.sec(plan.total_frames), 3),
        "final_seconds": round(plan.final_seconds, 3),
        "removed_seconds": round(plan.removed_seconds, 3),
        "cuts": [{"start": round(plan.sec(a), 3), "end": round(plan.sec(b), 3),
                  "start_tc": tc(plan.sec(a)), "end_tc": tc(plan.sec(b))} for a, b in plan.cuts],
        "keeps": [{"start": round(plan.sec(a), 3), "end": round(plan.sec(b), 3)} for a, b in plan.keeps],
        "skipped_long_silences": [{"start": round(s, 3), "end": round(e, 3),
                                   "start_tc": tc(s), "end_tc": tc(e)} for s, e in plan.skipped_long],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 4) 인코더 선택
# ---------------------------------------------------------------------------

@dataclass
class Encoder:
    name: str
    args: List[str]

    @property
    def is_hevc(self) -> bool:
        return "265" in self.name or "hevc" in self.name


HW_SUFFIXES = ("_nvenc", "_qsv", "_amf")
SW_PIX_FMTS = {"yuv420p", "yuv422p", "yuv444p", "yuv420p10le", "yuv422p10le", "yuv444p10le"}


def encoder_args(name: str, info: MediaInfo, crf: Optional[int], preset: Optional[str]) -> List[str]:
    hw = name.endswith(HW_SUFFIXES)
    ten_bit = "10" in info.pix_fmt
    hevc = "265" in name or "hevc" in name

    # 픽셀 포맷
    if hw:
        if ten_bit and hevc:
            pix = "p010le"
        else:
            pix = "nv12" if name.endswith("_qsv") else "yuv420p"
    else:
        pix = info.pix_fmt.replace("yuvj", "yuv")
        if pix not in SW_PIX_FMTS:
            pix = "yuv420p"

    args = ["-c:v", name, "-pix_fmt", pix]

    # 프리셋
    if name.endswith("_nvenc"):
        args += ["-preset", preset or "p5", "-tune", "hq"]
    elif name.endswith("_qsv"):
        args += ["-preset", preset or "medium"]
    elif name.endswith("_amf"):
        args += ["-quality", preset or "quality"]
    else:
        args += ["-preset", preset or "medium"]
        if name == "libx265":
            args += ["-x265-params", "log-level=error"]

    # 화질/비트레이트
    if crf is not None or not info.vbitrate:
        q = str(crf if crf is not None else 18)
        if name.endswith("_nvenc"):
            args += ["-rc", "vbr", "-cq", q, "-b:v", "0"]
        elif name.endswith("_qsv"):
            args += ["-global_quality", q]
        elif name.endswith("_amf"):
            args += ["-rc", "cqp", "-qp_i", q, "-qp_p", q]
        else:
            args += ["-crf", q]
    else:
        b = info.vbitrate
        rate = ["-b:v", str(b), "-maxrate", str(b * 2), "-bufsize", str(b * 2)]
        if name.endswith("_nvenc"):
            args += ["-rc", "vbr"] + rate
        elif name.endswith("_amf"):
            args += ["-rc", "vbr_peak"] + rate
        else:
            args += rate

    # GOP 2초 (유튜브 권장), 색 정보 유지
    args += ["-g", str(max(1, int(round(info.fps * 2))))]
    for key, opt in (("color_range", "-color_range"), ("color_space", "-colorspace"),
                     ("color_transfer", "-color_trc"), ("color_primaries", "-color_primaries")):
        if key in info.color:
            args += [opt, info.color[key]]
    if info.pix_fmt.startswith("yuvj") and "color_range" not in info.color:
        args += ["-color_range", "pc"]
    return args


_encoder_cache: Dict[Tuple[str, ...], bool] = {}


def encoder_works(tools: Tools, args: List[str]) -> bool:
    key = tuple(args)
    if key not in _encoder_cache:
        cmd = [tools.ffmpeg, "-hide_banner", "-v", "error", "-nostdin",
               "-f", "lavfi", "-i", "testsrc2=s=640x360:r=30:d=0.5", *args, "-f", "null", "-"]
        try:
            ok = subprocess.run(cmd, capture_output=True, timeout=60, **POPEN_KW).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        _encoder_cache[key] = ok
    return _encoder_cache[key]


def available_encoders(tools: Tools) -> set:
    out = subprocess.run([tools.ffmpeg, "-hide_banner", "-encoders"], capture_output=True,
                         text=True, encoding="utf-8", errors="replace", **POPEN_KW).stdout
    names = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            names.add(parts[1])
    return names


def choose_encoder(tools: Tools, info: MediaInfo, choice: str, crf: Optional[int],
                   preset: Optional[str]) -> Encoder:
    family = "hevc" if info.vcodec in ("hevc", "h265") else "h264"
    sw = "libx265" if family == "hevc" else "libx264"
    if choice == "auto":
        candidates = [f"{family}_nvenc", f"{family}_qsv", f"{family}_amf", sw, "libx264"]
    elif choice == "cpu":
        candidates = [sw, "libx264"]
    else:
        candidates = [choice, sw, "libx264"]
    have = available_encoders(tools)
    for name in dict.fromkeys(candidates):
        if name not in have:
            continue
        args = encoder_args(name, info, crf, preset)
        if encoder_works(tools, args):
            if choice not in ("auto", "cpu") and name != choice:
                print(f"  [경고] 인코더 '{choice}' 를 사용할 수 없어 {name} 로 대신 처리합니다.")
            return Encoder(name, args)
    raise SilenceCutError(f"사용 가능한 비디오 인코더가 없습니다 (시도: {', '.join(candidates)}).")


# ---------------------------------------------------------------------------
# 5) 렌더링
# ---------------------------------------------------------------------------

def audio_args(info: MediaInfo, track: int, final: bool) -> List[str]:
    a = info.audio[track]
    args = []
    if a.get("sample_rate"):
        args += ["-ar", str(a["sample_rate"])]
    if a.get("channels"):
        args += ["-ac", str(a["channels"])]
    if final:
        br = to_int(a.get("bit_rate")) or 192_000
        br = min(max(br, 128_000), 512_000)
        args = ["-c:a", "aac", "-b:a", str(br)] + args
    else:
        args = ["-c:a", "pcm_f32le"] + args  # 중간 파일은 무손실
    return args


def mp4_args(enc: Encoder) -> List[str]:
    args = ["-movflags", "+faststart", "-f", "mp4"]
    if enc.is_hevc:
        args = ["-tag:v", "hvc1"] + args
    return args


def write_filter_script(chunk: List[Tuple[int, int]], seek_f: int, plan: Plan, track: int,
                        path: Path) -> None:
    fps = plan.fps
    n = len(chunk)
    vs = "".join(f"[vs{i}]" for i in range(n))
    as_ = "".join(f"[as{i}]" for i in range(n))
    lines = [
        f"[0:v:0]fps={fps_str(fps)},split={n}{vs}" if n > 1 else f"[0:v:0]fps={fps_str(fps)}[vs0]",
        f"[0:a:{track}]asplit={n}{as_}" if n > 1 else f"[0:a:{track}]anull[as0]",
    ]
    for i, (a, b) in enumerate(chunk):
        ra, rb = a - seek_f, b - seek_f
        # 비디오: fps 필터 이후 타임베이스는 정확히 1/fps 이므로 PTS == 프레임 번호.
        # 초 단위 대신 정수 PTS 로 잘라 반올림 오차 없이 정확한 프레임을 선택한다.
        lines.append(f"[vs{i}]trim=start_pts={ra}:end_pts={rb},setpts=PTS-STARTPTS[v{i}]")
        a0, a1 = float(ra / fps), float(rb / fps)
        seg = a1 - a0
        fades = ""
        if seg > AUDIO_FADE * 4:
            if a > 0:
                fades += f",afade=t=in:st=0:d={AUDIO_FADE}"
            if b < plan.total_frames:
                fades += f",afade=t=out:st={seg - AUDIO_FADE:.6f}:d={AUDIO_FADE}"
        lines.append(f"[as{i}]atrim=start={a0:.6f}:end={a1:.6f},asetpts=PTS-STARTPTS{fades}[a{i}]")
    pairs = "".join(f"[v{i}][a{i}]" for i in range(n))
    lines.append(f"{pairs}concat=n={n}:v=1:a=1[vout][aout]")
    write_text_lf(path, ";\n".join(lines) + "\n")


def write_text_lf(path: Path, text: str) -> None:
    # Windows 에서도 \r\n 변환 없이 LF 로 기록 (ffmpeg 스크립트/리스트 파일용)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def render(tools: Tools, info: MediaInfo, plan: Plan, enc: Encoder, track: int,
           chunk_size: int, out_path: Path) -> None:
    fps = plan.fps
    chunks = [plan.keeps[i:i + chunk_size] for i in range(0, len(plan.keeps), chunk_size)]
    workdir = Path(tempfile.mkdtemp(prefix=".silence_cut_", dir=str(out_path.parent)))
    try:
        partial = workdir / "output.mp4"
        margin = int(round(SEEK_MARGIN * fps))
        chunk_files: List[str] = []
        for idx, chunk in enumerate(chunks):
            final = len(chunks) == 1
            seek_f = max(0, chunk[0][0] - margin)
            read_len = float((chunk[-1][1] - seek_f) / fps) + SEEK_MARGIN
            script = workdir / f"chunk_{idx:04d}.txt"
            write_filter_script(chunk, seek_f, plan, track, script)
            target = partial if final else workdir / f"chunk_{idx:04d}.mov"
            cmd = [tools.ffmpeg, "-hide_banner", "-y", "-v", "warning",
                   "-ss", f"{float(seek_f / fps):.6f}", "-t", f"{read_len:.6f}",
                   "-i", str(info.path),
                   tools.filter_script_opt(), str(script),
                   "-map", "[vout]", "-map", "[aout]",
                   *enc.args, "-r", fps_str(fps), *tools.cfr_opts(),
                   *audio_args(info, track, final)]
            cmd += mp4_args(enc) if final else ["-f", "mov"]
            cmd.append(str(target))
            label = "렌더링" if final else f"렌더링 {idx + 1}/{len(chunks)}"
            total = float(sum(b - a for a, b in chunk) / fps)
            run_ffmpeg(cmd, workdir / f"chunk_{idx:04d}.log", total, label)
            chunk_files.append(target.name)

        if len(chunks) > 1:
            listfile = workdir / "concat.txt"
            write_text_lf(listfile, "".join("file '{}'\n".format(n.replace("'", "'\\''"))
                                            for n in chunk_files))
            cmd = [tools.ffmpeg, "-hide_banner", "-y", "-v", "warning",
                   "-f", "concat", "-safe", "0", "-i", str(listfile),
                   "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                   *audio_args(info, track, True), *mp4_args(enc), str(partial)]
            run_ffmpeg(cmd, workdir / "concat.log", plan.final_seconds, "합치기")

        os.replace(partial, out_path)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def probe_duration(tools: Tools, path: Path) -> Optional[float]:
    res = subprocess.run([tools.ffprobe, "-v", "error", "-show_entries", "format=duration",
                          "-of", "default=nw=1:nk=1", str(path)],
                         capture_output=True, text=True, encoding="utf-8", errors="replace", **POPEN_KW)
    try:
        return float(res.stdout.strip())
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 파일 단위 처리
# ---------------------------------------------------------------------------

def process_file(tools: Tools, src: Path, out: Path, args: argparse.Namespace) -> Optional[dict]:
    print(f"\n[{src.name}]")
    if out.exists() and not args.overwrite and not args.dry_run:
        print(f"  출력 파일이 이미 있어 건너뜁니다: {out.name} (--overwrite 로 덮어쓰기)")
        return None
    started = time.monotonic()
    info = probe(tools, src, args.audio_track, args.fps)
    vfr_note = " (가변 프레임 -> 고정 프레임으로 변환)" if info.vfr else ""
    br = f", {info.vbitrate / 1000:.0f} kbps" if info.vbitrate else ""
    print(f"  원본: {info.width}x{info.height} {info.vcodec} {float(info.fps):.3f}fps{vfr_note}{br}")

    out.parent.mkdir(parents=True, exist_ok=True)
    detect_dir = Path(tempfile.mkdtemp(prefix=".silence_detect_"))
    try:
        silences = detect_silences(tools, info, args.db, args.min_silence, args.audio_track, detect_dir)
    finally:
        shutil.rmtree(detect_dir, ignore_errors=True)

    plan = build_plan(silences, info, args.padding, args.max_silence, args.min_keep)
    orig = plan.sec(plan.total_frames)

    print(f"  원본 길이      : {tc(orig)} ({orig:.1f}s)")
    print(f"  탐지된 무음    : {len(silences)}개")
    print(f"  잘라낼 구간    : {len(plan.cuts)}개")
    if plan.skipped_long:
        print(f"  [경고] {args.max_silence:g}초 이상 긴 무음 {len(plan.skipped_long)}개는 자르지 않았습니다 (수동 확인 권장):")
        for s, e in plan.skipped_long:
            print(f"         - {tc(s)} ~ {tc(e)} ({e - s:.1f}s)")
    print(f"  총 제거 시간   : {plan.removed_seconds:.1f}s")
    ratio = plan.final_seconds / orig * 100 if orig else 100
    print(f"  예상 최종 길이 : {tc(plan.final_seconds)} ({plan.final_seconds:.1f}s)"
          f"  -> 원본 대비 {ratio:.1f}% (-{100 - ratio:.1f}%)")

    if args.save_edl:
        edl_path = out.with_name(out.stem + ".edl.json")
        save_edl(plan, info, args, edl_path)
        print(f"  EDL 저장       : {edl_path.name}")

    if args.dry_run:
        for a, b in plan.cuts[:20]:
            print(f"         컷 {tc(plan.sec(a))} ~ {tc(plan.sec(b))} ({plan.sec(b - a):.2f}s)")
        if len(plan.cuts) > 20:
            print(f"         ... 외 {len(plan.cuts) - 20}개 (--save-edl 로 전체 목록 저장)")
        return {"name": src.name, "orig": orig, "final": plan.final_seconds,
                "cuts": len(plan.cuts), "dry_run": True}

    if not plan.cuts:
        print("  잘라낼 무음이 없어 출력 파일을 만들지 않습니다.")
        return None
    if not plan.keeps:
        print("  [경고] 전체가 무음으로 판정되었습니다. --db 값을 낮춰 보세요 (예: -40). 건너뜁니다.")
        return None

    enc = choose_encoder(tools, info, args.encoder, args.crf, args.preset)
    print(f"  인코더         : {enc.name}")
    render(tools, info, plan, enc, args.audio_track, args.chunk_size, out)

    final = probe_duration(tools, out) or plan.final_seconds
    ratio = final / orig * 100 if orig else 100
    elapsed = time.monotonic() - started
    print(f"  최종 길이      : {tc(final)} ({final:.1f}s)  -> 원본 대비 {ratio:.1f}%")
    print(f"  출력           : {out}")
    print(f"  처리 시간      : {tc(elapsed)}")
    return {"name": src.name, "orig": orig, "final": final, "cuts": len(plan.cuts)}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def collect_jobs(inp: Path, output: Optional[Path]) -> List[Tuple[Path, Path]]:
    if inp.is_dir():
        files = sorted(p for p in inp.iterdir()
                       if p.is_file() and p.suffix.lower() == ".mp4" and not p.stem.endswith("_cut"))
        out_dir = output or inp
        return [(p, out_dir / f"{p.stem}_cut.mp4") for p in files]
    if not inp.is_file():
        raise SilenceCutError(f"입력을 찾을 수 없습니다: {inp}")
    if output is None:
        out = inp.with_name(f"{inp.stem}_cut.mp4")
    elif output.is_dir():
        out = output / f"{inp.stem}_cut.mp4"
    else:
        out = output
    return [(inp, out)]


def positive_fraction(value: str) -> Fraction:
    f = parse_fraction(value)
    if f is None:
        raise argparse.ArgumentTypeError("예: 30, 60, 30000/1001")
    return f


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="롱폼 영상의 무음 구간을 자동으로 잘라내 _cut.mp4 로 저장합니다.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--input", "-i", required=True, type=Path, help="mp4 파일 또는 mp4가 든 폴더")
    p.add_argument("--output", "-o", type=Path,
                   help="출력 파일 경로 (폴더 입력 시 출력 폴더). 생략하면 원본 옆에 '_cut' 접미사")
    p.add_argument("--db", type=float, default=-30.0, help="무음 판정 임계값 (dB)")
    p.add_argument("--min-silence", type=float, default=0.5, help="이보다 짧은 무음은 무시 (초)")
    p.add_argument("--max-silence", type=float, default=10.0,
                   help="이 이상 긴 무음은 자르지 않고 경고만 (초, 0=제한 없음)")
    p.add_argument("--padding", type=float, default=0.12, help="무음 앞뒤로 남길 여유 (초)")
    p.add_argument("--min-keep", type=float, default=0.1, help="이보다 짧게 남는 조각은 함께 제거 (초)")
    p.add_argument("--audio-track", type=int, default=0, help="탐지/출력에 사용할 오디오 트랙 번호 (0부터)")
    p.add_argument("--encoder", default="auto",
                   help="auto(GPU 자동 탐지 후 CPU), cpu, 또는 ffmpeg 인코더 이름 (h264_nvenc, libx264 등)")
    p.add_argument("--crf", type=int, help="지정 시 원본 비트레이트 대신 화질 고정 모드 (낮을수록 고화질, 18 권장)")
    p.add_argument("--preset", help="인코더 프리셋 (기본: libx264=medium, nvenc=p5)")
    p.add_argument("--fps", type=positive_fraction, help="출력 프레임레이트 강제 지정 (예: 60, 30000/1001)")
    p.add_argument("--chunk-size", type=int, default=80, help="한 번에 렌더링할 구간 수")
    p.add_argument("--dry-run", action="store_true", help="렌더링 없이 탐지 결과와 EDL(json)만 출력")
    p.add_argument("--save-edl", action="store_true", help="편집 리스트를 <출력>.edl.json 으로 저장")
    p.add_argument("--overwrite", action="store_true", help="기존 출력 파일 덮어쓰기")
    p.add_argument("--ffmpeg", help="ffmpeg 실행 파일 경로 (PATH 에 없을 때)")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if args.padding < 0 or args.min_silence <= 0 or args.chunk_size < 1:
        print("[오류] --padding >= 0, --min-silence > 0, --chunk-size >= 1 이어야 합니다.", file=sys.stderr)
        return 2

    try:
        tools = find_tools(args.ffmpeg)
        jobs = collect_jobs(args.input, args.output)
    except SilenceCutError as e:
        print(f"[오류] {e}", file=sys.stderr)
        return 2
    if not jobs:
        print("처리할 mp4 파일이 없습니다.")
        return 1

    results, failed = [], []
    for src, out in jobs:
        if src.resolve() == out.resolve():
            print(f"\n[{src.name}] 출력 경로가 원본과 같아 건너뜁니다.")
            continue
        try:
            r = process_file(tools, src, out, args)
            if r:
                results.append(r)
        except SilenceCutError as e:
            print(f"  [오류] {e}", file=sys.stderr)
            failed.append(src.name)
        except KeyboardInterrupt:
            print("\n중단되었습니다. (원본은 변경되지 않았고, 미완성 출력은 삭제되었습니다)")
            return 130

    if len(jobs) > 1 and (results or failed):
        print("\n==== 일괄 처리 요약 ====")
        for r in results:
            print(f"  {r['name']}: {tc(r['orig'])} -> {tc(r['final'])}  (컷 {r['cuts']}개)")
        if failed:
            print(f"  실패: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
