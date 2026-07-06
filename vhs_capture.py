"""
VHS Capture — records webcam to AV1 and auto-splits on blue screen detection.
Requires: pip install PyQt6 opencv-python numpy sounddevice imageio-ffmpeg
FFmpeg is bundled via imageio-ffmpeg if not found on system PATH.
"""

import sys
import os
import cv2
import numpy as np
import subprocess
import threading
import time
import json
import shutil
import queue as _queue
from datetime import datetime, timedelta
from pathlib import Path

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QGroupBox, QTextEdit, QComboBox,
    QSpinBox, QFileDialog, QFrame, QProgressBar, QCheckBox,
    QGridLayout, QDialog, QDialogButtonBox, QListWidget,
    QListWidgetItem, QLineEdit, QScrollArea, QSizePolicy
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QMutex, QMutexLocker
)
from PyQt6.QtGui import (
    QImage, QPixmap, QColor, QPalette, QTextCursor, QPainter, QPen
)

try:
    import sounddevice as sd
    _AUDIO_AVAILABLE = True
except Exception:
    _AUDIO_AVAILABLE = False

# ── Resolve FFmpeg binary ──────────────────────────────────────────────────
# Priority: system PATH → imageio-ffmpeg bundled binary → error at runtime
def _find_ffmpeg() -> str:
    """Return the path to a working ffmpeg binary."""
    # 1. Check if ffmpeg is already on PATH
    if shutil.which("ffmpeg"):
        return "ffmpeg"
    # 2. Try imageio-ffmpeg bundled binary (works inside PyInstaller .exe too)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    # 3. Give up — callers will get a clear error from subprocess
    return "ffmpeg"

FFMPEG_BIN = _find_ffmpeg()

# ── Try to load the Rust extension; fall back to numpy gracefully ──────────
try:
    import vhs_core as _vhs_core
    _RUST_AVAILABLE = True
except ImportError:
    _vhs_core = None
    _RUST_AVAILABLE = False


def analyse_blue_ratio(frame, sample_w=80, sample_h=45):
    """
    Return fraction of pixels in frame that are 'VHS blue'.
    Uses the Rust extension (vhs_core) when available — skips the cv2.resize
    step entirely by doing the downsample inside Rust, saving one allocation.
    Falls back to numpy when the extension isn't installed.
    """
    if _RUST_AVAILABLE:
        h, w = frame.shape[:2]
        # Pass raw bytes directly — no copy made by numpy here
        return _vhs_core.detect_blue(
            frame.tobytes(), w, h, 0.0, sample_w, sample_h
        )

    # ── numpy fallback ────────────────────────────────────────────────
    small = cv2.resize(frame, (sample_w, sample_h))
    b_ch  = small[:, :, 0].astype(np.float32)
    g_ch  = small[:, :, 1].astype(np.float32)
    r_ch  = small[:, :, 2].astype(np.float32)
    maxc  = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    minc  = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    d     = maxc - minc + 1e-6
    sat   = np.where(maxc > 0, d / maxc, 0)
    hue   = np.zeros_like(maxc)
    mask_r = maxc == r_ch
    mask_g = (maxc == g_ch) & ~mask_r
    mask_b = ~mask_r & ~mask_g
    hue[mask_r] = (60 * ((g_ch[mask_r] - b_ch[mask_r]) / d[mask_r]) + 360) % 360
    hue[mask_g] =  60 * ((b_ch[mask_g] - r_ch[mask_g]) / d[mask_g]) + 120
    hue[mask_b] =  60 * ((r_ch[mask_b] - g_ch[mask_b]) / d[mask_b]) + 240
    blue_mask = (hue >= 190) & (hue <= 265) & (sat >= 0.20) & (b_ch > 60)
    return float(blue_mask.sum()) / (sample_w * sample_h)


# ─────────────────────────────────────────────
#  Settings persistence
# ─────────────────────────────────────────────

_SETTINGS_PATH = Path.home() / ".vhs_capture_settings.json"

def load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_PATH.read_text())
    except Exception:
        return {}

def save_settings(d: dict):
    try:
        _SETTINGS_PATH.write_text(json.dumps(d, indent=2))
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Audio meter thread
# ─────────────────────────────────────────────

class AudioMeterThread(QThread):
    """Reads microphone/line-in and emits RMS level 20x/sec."""
    level = pyqtSignal(float)   # 0.0–1.0

    def __init__(self):
        super().__init__()
        self._running = False

    def run(self):
        if not _AUDIO_AVAILABLE:
            return
        self._running = True
        block = int(sd.default.samplerate or 44100) // 20  # 50ms blocks
        try:
            with sd.InputStream(channels=1, blocksize=block,
                                dtype='float32') as stream:
                while self._running:
                    data, _ = stream.read(block)
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    # Normalise: -60dBFS → 0, 0dBFS → 1
                    db  = max(-60.0, 20 * np.log10(rms + 1e-9))
                    lvl = (db + 60) / 60
                    self.level.emit(min(1.0, max(0.0, lvl)))
        except Exception:
            pass

    def stop(self):
        self._running = False
        self.wait(1000)


# ─────────────────────────────────────────────
#  Hardware encoder detection + profiles
# ─────────────────────────────────────────────

def _system_ffmpeg() -> str | None:
    """System FFmpeg on PATH — needed for hw encoders (NVENC/QSV)."""
    import shutil as _sh
    return _sh.which("ffmpeg")


# Full encoder profiles. Each entry:
#   name, label, is_hw, pixfmt,
#   requires_hw_ffmpeg  — True means imageio-ffmpeg won't have it,
#                         only attempt when system FFmpeg is available
#   args(crf, w, h)     — extra flags after "-c:v <name>"
_ENCODER_PROFILES = [
    # ── NVIDIA NVENC ─────────────────────────────────────────────────
    {
        "name":  "av1_nvenc",
        "label": "AV1  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        # constqp = constant QP (same concept as CRF); p4 = balanced
        "args": lambda q, w, h: ["-rc", "constqp", "-qp", str(q),
                                  "-preset", "p4"],
    },
    {
        "name":  "hevc_nvenc",
        "label": "HEVC  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "args": lambda q, w, h: ["-rc", "constqp", "-qp", str(q),
                                  "-preset", "p4"],
    },
    {
        "name":  "h264_nvenc",
        "label": "H.264  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "args": lambda q, w, h: ["-rc", "constqp", "-qp", str(q),
                                  "-preset", "p4"],
    },
    # ── Intel QuickSync ──────────────────────────────────────────────
    {
        "name":  "av1_qsv",
        "label": "AV1  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "nv12",   # QSV requires nv12 input
        "requires_hw_ffmpeg": True,
        # preset 4 = medium (0=best…7=fastest); look_ahead_depth 0 = low latency
        "args": lambda q, w, h: ["-global_quality", str(q),
                                  "-look_ahead_depth", "0", "-preset", "4"],
    },
    {
        "name":  "hevc_qsv",
        "label": "HEVC  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "nv12",
        "requires_hw_ffmpeg": True,
        "args": lambda q, w, h: ["-global_quality", str(q),
                                  "-look_ahead_depth", "0", "-preset", "4"],
    },
    {
        "name":  "h264_qsv",
        "label": "H.264  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "nv12",
        "requires_hw_ffmpeg": True,
        "args": lambda q, w, h: ["-global_quality", str(q),
                                  "-look_ahead_depth", "0", "-preset", "4"],
    },
    # ── Software (bundled FFmpeg works fine for these) ───────────────
    {
        "name":  "libsvtav1",
        "label": "AV1  —  SVT-AV1 (CPU fast)",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "args": lambda q, w, h: ["-preset", "6", "-crf", str(q)],
    },
    {
        "name":  "libaom-av1",
        "label": "AV1  —  libaom (CPU best quality)",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "args": lambda q, w, h: ["-cpu-used", "6", "-b:v", "0", "-crf", str(q)],
    },
    {
        "name":  "libx265",
        "label": "HEVC / H.265  —  CPU",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "args": lambda q, w, h: ["-preset", "fast", "-crf", str(q)],
    },
    {
        "name":  "libx264",
        "label": "H.264  —  CPU",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "args": lambda q, w, h: ["-preset", "fast", "-crf", str(q)],
    },
]


def _friendly_hw_error(stderr: str) -> str:
    s = stderr.lower()
    if "libcuda" in s or "cannot load" in s:
        return "NVIDIA driver not found — install Game Ready or Studio driver"
    if "mfx session" in s or "mfxinit" in s or "error creating" in s:
        return "Intel MFX session failed — update Intel graphics driver"
    if "no capable devices" in s or "device open failed" in s:
        return "GPU device unavailable"
    if "unsupported" in s or "not support" in s:
        return "GPU too old for this codec"
    # Return the most useful line from stderr
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if line and ("error" in line.lower() or "fail" in line.lower()):
            return line[:120]
    return "unknown error"


def probe_encoders():
    """
    Test each encoder profile by piping a real rawvideo frame.
    Returns (working: list[dict], failed_hw: list[dict]).
    hw encoders use system FFmpeg; sw encoders use whichever is available.
    Each working profile has 'ffmpeg_bin' set to the binary that worked.
    Each failed hw profile has 'fail_reason' explaining why.
    """
    sys_ff  = _system_ffmpeg()
    bnd_ff  = FFMPEG_BIN
    working = []
    failed  = []
    black   = bytes(64 * 64 * 3)   # 64×64 black BGR frame

    for profile in _ENCODER_PROFILES:
        enc       = profile["name"]
        pixfmt    = profile["pixfmt"]
        args      = profile["args"](28, 64, 64)
        needs_sys = profile.get("requires_hw_ffmpeg", False)

        ff = sys_ff if needs_sys else bnd_ff
        if ff is None:
            if profile["is_hw"]:
                failed.append({**profile,
                    "fail_reason": "system FFmpeg not found — install ffmpeg"})
            continue

        cmd = [
            ff, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", "64x64", "-r", "25", "-i", "pipe:0",
        ]
        if pixfmt == "nv12":
            cmd += ["-vf", "format=nv12"]
        cmd += ["-c:v", enc] + args + ["-pix_fmt", pixfmt,
                "-frames:v", "1", "-f", "null", "-"]

        try:
            r = subprocess.run(cmd, input=black, capture_output=True, timeout=12)
            if r.returncode == 0:
                p = dict(profile)
                p["ffmpeg_bin"] = ff
                working.append(p)
            elif profile["is_hw"]:
                reason = _friendly_hw_error(r.stderr.decode(errors="replace"))
                failed.append({**profile, "fail_reason": reason})
        except subprocess.TimeoutExpired:
            if profile["is_hw"]:
                failed.append({**profile, "fail_reason": "probe timed out"})
        except FileNotFoundError:
            break   # FFmpeg binary missing entirely

    return working, failed


# ─────────────────────────────────────────────
#  FFmpeg pipe writer (runs in background thread)
# ─────────────────────────────────────────────

class FFmpegWriter:
    """Wraps an ffmpeg process that accepts raw BGR frames on stdin."""

    def __init__(self, output_path: str, width: int, height: int, fps: float,
                 encoder: str = "libsvtav1", crf: int = 28,
                 encoder_info: dict | None = None):
        self.output_path  = output_path
        self.width        = width
        self.height       = height
        self.fps          = fps
        self.encoder      = encoder
        self.crf          = crf
        self.encoder_info = encoder_info
        self._proc        = None
        self._lock        = threading.Lock()

    def start(self):
        info    = self.encoder_info or {}
        enc     = self.encoder
        pixfmt  = info.get("pixfmt", "yuv420p")
        is_hw   = info.get("is_hw", False)
        ff      = info.get("ffmpeg_bin", FFMPEG_BIN)  # hw encoders use system FF

        args_fn = info.get("args")
        enc_args = args_fn(self.crf, self.width, self.height) if args_fn \
                   else ["-crf", str(self.crf)]

        cmd = [
            ff, "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
        ]
        # QSV needs nv12; insert a pixel-format conversion filter
        if is_hw and pixfmt == "nv12":
            cmd += ["-vf", "format=nv12"]

        cmd += ["-c:v", enc] + enc_args + ["-pix_fmt", pixfmt]
        cmd += ["-movflags", "+faststart", self.output_path]

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE   # capture for error reporting
        )
        # Drain stderr in background so it never blocks
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        if self._proc and self._proc.stderr:
            for line in self._proc.stderr:
                decoded = line.decode(errors="replace").strip()
                if decoded:
                    self._stderr_lines.append(decoded)

    def last_errors(self, n=5) -> list[str]:
        """Return the last N stderr lines from FFmpeg (for error reporting)."""
        return self._stderr_lines[-n:]

    def write(self, frame: np.ndarray):
        with self._lock:
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    pass

    def write_raw(self, raw: bytes):
        with self._lock:
            if self._proc and self._proc.stdin:
                try:
                    self._proc.stdin.write(raw)
                except BrokenPipeError:
                    pass

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    self._proc.stdin.close()
                except Exception:
                    pass
                self._proc.wait(timeout=30)
                self._proc = None

    @property
    def running(self):
        return self._proc is not None


# ─────────────────────────────────────────────
#  Capture thread
# ─────────────────────────────────────────────

import queue as _queue

# Preview is throttled to this many fps to keep the UI thread free.
_PREVIEW_FPS = 25
# Blue detection runs every Nth capture frame (saves ~60% CPU vs every frame).
_DETECT_EVERY_N = 3


class _WriterThread(threading.Thread):
    """
    Dedicated thread that owns the FFmpegWriter and drains a bounded queue.
    Uses vhs_core.FrameQueue (Rust) when available; falls back to queue.Queue.
    """
    def __init__(self, capacity=12, on_error=None):
        super().__init__(daemon=True)
        if _RUST_AVAILABLE:
            self._q = _vhs_core.FrameQueue(capacity)
            self._use_rust = True
        else:
            self._q = _queue.Queue(maxsize=capacity)
            self._use_rust = False
        self._writer: FFmpegWriter | None = None
        self._lock = threading.Lock()
        self._running = True
        self._on_error = on_error  # callable(list[str]) for FFmpeg errors

    def set_writer(self, w: FFmpegWriter | None):
        with self._lock:
            old = self._writer
            self._writer = w
        if old:
            def _stop_and_report():
                old.stop()
                if self._on_error:
                    errs = old.last_errors()
                    if errs:
                        self._on_error(errs)
            threading.Thread(target=_stop_and_report, daemon=True).start()

    def push(self, frame: np.ndarray):
        """Non-blocking push; drops frame if queue is full."""
        raw = frame.tobytes()
        if self._use_rust:
            self._q.push(raw)          # returns bool, we ignore it
        else:
            try:
                self._q.put_nowait(raw)
            except _queue.Full:
                pass

    def run(self):
        while self._running:
            if self._use_rust:
                raw = self._q.pop()
                if raw is None:
                    time.sleep(0.002)  # brief yield when empty
                    continue
            else:
                try:
                    raw = self._q.get(timeout=0.05)
                except _queue.Empty:
                    continue

            with self._lock:
                w = self._writer
            if w:
                w.write_raw(raw)

    def stop(self):
        self._running = False
        self.join(timeout=5)
        with self._lock:
            if self._writer:
                self._writer.stop()
                self._writer = None


class CaptureThread(QThread):
    frame_ready    = pyqtSignal(object)           # preview frame (BGR ndarray)
    blue_changed   = pyqtSignal(bool, float)      # is_blue, ratio
    segment_split  = pyqtSignal(str, float)       # output path, timestamp
    timecode_tick  = pyqtSignal(float)            # seconds elapsed
    error          = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self.cfg = config
        self._running = False
        self._recording = False
        self._segment_index = 0
        self._start_time = 0.0
        self._blue_frames = 0
        self._in_blue = False
        self._blue_hold_frames = 0
        self._mutex = QMutex()
        self._writer_thread = _WriterThread(
            on_error=lambda errs: self.error.emit("FFmpeg: " + errs[-1])
        )
        self._writer_thread.start()

    def start_recording(self):
        with QMutexLocker(self._mutex):
            self._segment_index = 0
            self._start_time = time.time()
            self._recording = True
            self._start_new_segment()

    def stop_recording(self):
        with QMutexLocker(self._mutex):
            self._recording = False
        self._writer_thread.set_writer(None)

    def manual_split(self):
        with QMutexLocker(self._mutex):
            if self._recording:
                self._start_new_segment()

    def _start_new_segment(self):
        cfg = self.cfg
        out_dir = Path(cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = "mp4" if cfg["encoder"] in ("libsvtav1", "libaom-av1", "libx265", "libx264") else "mkv"
        title  = cfg.get("title_prefix", "").strip()
        part   = cfg.get("part_prefix", "").strip()
        clip_n = f"clip{self._segment_index+1:02d}"
        parts  = [p for p in [title, part, clip_n, ts] if p]
        fname  = out_dir / f"{'_'.join(parts)}.{ext}"
        self._segment_index += 1
        w = FFmpegWriter(
            str(fname),
            cfg["width"], cfg["height"], cfg["fps"],
            encoder=cfg["encoder"], crf=cfg["crf"],
            encoder_info=cfg.get("encoder_info"),
        )
        w.start()
        self._writer_thread.set_writer(w)
        elapsed = time.time() - self._start_time if self._start_time else 0.0
        self.segment_split.emit(str(fname), elapsed)

    def run(self):
        cfg = self.cfg

        cap = cv2.VideoCapture(cfg["camera_index"])
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
        cap.set(cv2.CAP_PROP_FPS,          cfg["fps"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            self.error.emit("Could not open camera. Check the camera index.")
            return

        self._running = True
        preview_interval  = 1.0 / _PREVIEW_FPS
        timecode_interval = 0.5

        last_preview  = 0.0
        last_timecode = 0.0
        frame_count   = 0

        # Time-based blue detection state (seconds, not frames)
        blue_start_time: float | None = None   # when continuous blue began
        resume_start_time: float | None = None # when non-blue after blue began
        MIN_BLUE_SECS    = cfg["min_blue_seconds"]
        HYSTERESIS_SECS  = 0.5   # must be non-blue for 0.5s before resuming

        while self._running:
            ret, frame = cap.read()
            if not ret:
                time.sleep(0.005)
                continue

            now = time.time()
            frame_count += 1

            # ── Throttled preview emit ───────────────────────
            if now - last_preview >= preview_interval:
                last_preview = now
                preview = cv2.resize(frame, (640, 360),
                                     interpolation=cv2.INTER_LINEAR)
                self.frame_ready.emit(preview)

            # ── Blue detection (every Nth frame) ────────────
            if frame_count % _DETECT_EVERY_N == 0:
                ratio      = analyse_blue_ratio(frame)
                is_blue_now = ratio >= cfg["blue_threshold"]
                auto_split  = cfg.get("auto_split", True)

                if is_blue_now:
                    resume_start_time = None          # reset resume timer
                    if blue_start_time is None:
                        blue_start_time = now         # start blue timer

                    blue_secs = now - blue_start_time
                    if blue_secs >= MIN_BLUE_SECS and not self._in_blue:
                        # Blue screen confirmed
                        self._in_blue = True
                        self.blue_changed.emit(True, ratio)
                        if auto_split:
                            with QMutexLocker(self._mutex):
                                if self._recording:
                                    # Close current clip; new one starts on resume
                                    self._writer_thread.set_writer(None)
                else:
                    blue_start_time = None            # reset blue timer

                    if self._in_blue:
                        # We were in blue — start counting resume time
                        if resume_start_time is None:
                            resume_start_time = now
                        elif now - resume_start_time >= HYSTERESIS_SECS:
                            # Content has genuinely resumed
                            self._in_blue = False
                            resume_start_time = None
                            self.blue_changed.emit(False, ratio)
                            if auto_split:
                                with QMutexLocker(self._mutex):
                                    if self._recording:
                                        self._start_new_segment()

            # ── Write frame to encoder queue ─────────────────
            with QMutexLocker(self._mutex):
                rec = self._recording
            # Always write unless we're in a confirmed blue screen
            # (even if auto_split is off, we still skip the blue garbage)
            if rec and not self._in_blue:
                self._writer_thread.push(frame)

            # ── Timecode ─────────────────────────────────────
            if now - last_timecode >= timecode_interval:
                last_timecode = now
                elapsed = now - self._start_time if self._start_time else 0.0
                self.timecode_tick.emit(elapsed)

        cap.release()

    def stop(self):
        self._running = False
        self.stop_recording()
        self._writer_thread.stop()
        self.wait(5000)


# ─────────────────────────────────────────────
#  Event log model
# ─────────────────────────────────────────────

class EventLog:
    def __init__(self):
        self.events = []

    def add(self, kind: str, message: str, detail: str = ""):
        ts = datetime.now().strftime("%H:%M:%S")
        self.events.append({"ts": ts, "kind": kind, "msg": message, "detail": detail})

    def format_html(self):
        colours = {
            "info":    "#378add",
            "blue":    "#5b9bd5",
            "resume":  "#639922",
            "split":   "#ba7517",
            "error":   "#e24b4a",
            "rec":     "#3b6d11",
            "stop":    "#888780",
        }
        lines = []
        for e in self.events:
            col = colours.get(e["kind"], "#888")
            detail = f" <span style='color:#888'>{e['detail']}</span>" if e["detail"] else ""
            lines.append(
                f"<span style='color:#888'>[{e['ts']}]</span> "
                f"<span style='color:{col};font-weight:bold'>{e['msg']}</span>"
                f"{detail}"
            )
        return "<br>".join(lines)


# ─────────────────────────────────────────────
#  Camera picker dialog
# ─────────────────────────────────────────────

def _probe_camera(index: int) -> dict | None:
    """
    Open a camera, probe its maximum supported resolution by trying common
    sizes from highest to lowest, and return a dict with index/label/w/h.
    Returns None if the camera can't be opened.
    """
    PROBE_RESOLUTIONS = [
        (3840, 2160),
        (1920, 1080),
        (1280,  720),
        ( 640,  480),
        ( 320,  240),
    ]
    cap = cv2.VideoCapture(index)   # default backend — DSHOW via index is broken on many systems
    if not cap.isOpened():
        return None

    best_w, best_h = 0, 0
    for tw, th in PROBE_RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  tw)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, th)
        got_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        got_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        if got_w * got_h > best_w * best_h:
            best_w, best_h = got_w, got_h

    fps = cap.get(cv2.CAP_PROP_FPS)
    backend_name = cap.getBackendName() if hasattr(cap, "getBackendName") else ""
    cap.release()

    if best_w == 0:
        return None

    label = f"Camera {index}  —  {best_w}×{best_h}"
    if fps > 0:
        label += f" @ {fps:.0f}fps"
    if backend_name:
        label += f"  [{backend_name}]"

    return {"index": index, "label": label, "w": best_w, "h": best_h, "fps": fps}


def _enumerate_cameras(max_index=8):
    """Return list of camera info dicts for all working cameras."""
    found = []
    for i in range(max_index):
        info = _probe_camera(i)
        if info:
            found.append(info)
    return found


class CameraPickerDialog(QDialog):
    # Signal used to safely deliver scan results back to the UI thread
    _scan_done = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select camera")
        self.setMinimumWidth(460)
        self.setStyleSheet(
            f"QDialog{{background:#1a1a1a;color:#e8e8e8}}"
            f"QLabel{{color:#e8e8e8}}"
        )
        self.selected_index = 0
        self.selected_w = 1280
        self.selected_h = 720
        self.selected_fps = 25.0
        self._cameras = []

        self._scan_done.connect(self._populate)

        lay = QVBoxLayout(self)
        lay.setSpacing(12)
        lay.setContentsMargins(16, 16, 16, 16)

        self.lbl_hdr = QLabel("Scanning for cameras — this may take a few seconds…")
        self.lbl_hdr.setStyleSheet("color:#888;font-size:12px")
        lay.addWidget(self.lbl_hdr)

        self.list = QListWidget()
        self.list.setStyleSheet(
            f"QListWidget{{background:#0f0f0f;border:1px solid #2a2a2a;border-radius:8px;"
            f"color:#e8e8e8;font-size:13px;padding:4px}}"
            f"QListWidget::item{{padding:8px 10px;border-radius:6px}}"
            f"QListWidget::item:selected{{background:#0c2a42;color:#fff}}"
            f"QListWidget::item:hover{{background:#252525}}"
        )
        self.list.setMinimumHeight(160)
        lay.addWidget(self.list)

        self.lbl_status = QLabel("")
        self.lbl_status.setStyleSheet("color:#888;font-size:12px")
        lay.addWidget(self.lbl_status)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.setStyleSheet(
            f"QPushButton{{background:#252525;color:#e8e8e8;border:1px solid #2a2a2a;"
            f"border-radius:6px;padding:5px 18px;font-size:13px}}"
            f"QPushButton:hover{{background:#303030}}"
        )
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        lay.addWidget(btns)

        from PyQt6.QtCore import QTimer
        QTimer.singleShot(50, self._scan)

    def _scan(self):
        sig = self._scan_done
        def worker():
            cameras = _enumerate_cameras()
            sig.emit(cameras)
        threading.Thread(target=worker, daemon=True).start()

    def _populate(self, cameras):
        self._cameras = cameras
        self.list.clear()
        if not cameras:
            self.lbl_hdr.setText("No cameras found.")
            return
        for info in cameras:
            item = QListWidgetItem(info["label"])
            item.setData(Qt.ItemDataRole.UserRole, info)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        self.lbl_hdr.setText("Select a camera:")
        self.lbl_status.setText(f"{len(cameras)} camera(s) found")

    def _accept(self):
        item = self.list.currentItem()
        if item:
            info = item.data(Qt.ItemDataRole.UserRole)
            self.selected_index = info["index"]
            self.selected_w     = info["w"]
            self.selected_h     = info["h"]
            self.selected_fps   = info["fps"]
        self.accept()


# ─────────────────────────────────────────────
#  Style constants (web-UI palette)
# ─────────────────────────────────────────────

BG       = "#0f0f0f"   # page background
SURFACE  = "#1a1a1a"   # card / panel
BORDER   = "#2a2a2a"   # subtle border
TEXT     = "#e8e8e8"   # primary text
MUTED    = "#888888"   # secondary text
BLUE     = "#378add"   # info / blue badge
BLUE_BG  = "#0c2a42"
GREEN    = "#639922"   # success / rec
GREEN_BG = "#1a2e0a"
RED      = "#e24b4a"   # danger / stop
RED_BG   = "#2e0a0a"
AMBER    = "#ba7517"
AMBER_BG = "#2e1f0a"

def _pill(text: str, fg: str, bg: str) -> QLabel:
    """Create a small rounded badge label."""
    lbl = QLabel(text)
    lbl.setStyleSheet(
        f"color:{fg};background:{bg};border-radius:10px;"
        f"padding:2px 10px;font-size:11px;font-weight:600;"
    )
    lbl.setFixedHeight(20)
    return lbl


def _card(parent=None) -> QFrame:
    f = QFrame(parent)
    f.setStyleSheet(
        f"QFrame{{background:{SURFACE};border:1px solid {BORDER};"
        f"border-radius:10px;}}"
    )
    return f


# ─────────────────────────────────────────────
#  Main window
# ─────────────────────────────────────────────

class VHSCapture(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VHS Capture")
        self.setMinimumSize(1180, 740)
        self._capture: CaptureThread | None = None
        self._audio_meter: AudioMeterThread | None = None
        self._recording = False
        self._log = EventLog()
        self._clips: list[str] = []
        self._seg_count = 0
        self._in_blue = False
        self._camera_index = 0
        self._camera_native_w = 1280
        self._camera_native_h = 720
        self._camera_native_fps = 25.0

        # Load persisted settings
        cfg = load_settings()
        self._output_dir = cfg.get("output_dir", str(Path.home() / "VHS_Captures"))

        self._build_ui()
        self._restore_settings(cfg)
        self._apply_windows_theme()

        if _RUST_AVAILABLE:
            self._log_event("info", "vhs_core (Rust) loaded",
                            "blue detection + BGR→RGB + FrameQueue active")
        else:
            self._log_event("stop", "vhs_core not found",
                            "run build_windows.bat then pip install the .whl")
        if not _AUDIO_AVAILABLE:
            self._log_event("stop", "sounddevice not found",
                            "pip install sounddevice  (audio meter disabled)")

        # FFmpeg availability
        bundled = FFMPEG_BIN != "ffmpeg"
        try:
            r = subprocess.run([FFMPEG_BIN, "-version"], capture_output=True, timeout=5)
            ff_ok = r.returncode == 0
        except Exception:
            ff_ok = False
        if ff_ok:
            self._log_event("info",
                            f"FFmpeg found {'(bundled)' if bundled else '(on PATH)'}",
                            FFMPEG_BIN)
        else:
            self._log_event("stop", "FFmpeg not found",
                            "recording will fail — install FFmpeg or run build_windows.bat")

    # ── Reusable widget builders ─────────────

    def _make_btn(self, text, primary=False, danger=False):
        b = QPushButton(text)
        b.setFixedHeight(36)
        base = f"border:1px solid {BORDER};border-radius:8px;padding:0 18px;font-size:13px;font-weight:500;"
        if primary:
            b.setStyleSheet(
                f"QPushButton{{{base}background:{SURFACE};color:{TEXT};}}"
                f"QPushButton:hover{{background:#252525;border-color:#444;}}"
                f"QPushButton:disabled{{color:#444;border-color:{BORDER};}}"
            )
        elif danger:
            b.setStyleSheet(
                f"QPushButton{{{base}background:{RED_BG};color:{RED};border-color:{RED};}}"
                f"QPushButton:hover{{background:#3a0e0e;}}"
                f"QPushButton:disabled{{color:#444;border-color:{BORDER};}}"
            )
        else:
            b.setStyleSheet(
                f"QPushButton{{{base}background:transparent;color:{MUTED};}}"
                f"QPushButton:hover{{background:{SURFACE};color:{TEXT};}}"
                f"QPushButton:disabled{{color:#444;border-color:{BORDER};}}"
            )
        return b

    def _make_slider_row(self, label_text, lo, hi, val, fmt_fn):
        row = QHBoxLayout()
        lbl = QLabel(label_text)
        lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:140px")
        sl = QSlider(Qt.Orientation.Horizontal)
        sl.setRange(lo, hi)
        sl.setValue(val)
        sl.setStyleSheet(
            f"QSlider::groove:horizontal{{height:4px;background:{BORDER};border-radius:2px;}}"
            f"QSlider::handle:horizontal{{width:14px;height:14px;margin:-5px 0;"
            f"background:{BLUE};border-radius:7px;}}"
            f"QSlider::sub-page:horizontal{{background:{BLUE};border-radius:2px;}}"
        )
        val_lbl = QLabel(fmt_fn(val))
        val_lbl.setStyleSheet(f"color:{TEXT};font-size:13px;font-weight:500;min-width:38px;text-align:right")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sl.valueChanged.connect(lambda v: val_lbl.setText(fmt_fn(v)))
        row.addWidget(lbl)
        row.addWidget(sl, 1)
        row.addWidget(val_lbl)
        return row, sl, val_lbl

    def _section(self, title):
        """Titled card container, returns (card_widget, inner_layout)."""
        card = _card()
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 14, 16, 16)
        lay.setSpacing(10)
        hdr = QLabel(title)
        hdr.setStyleSheet(f"color:{MUTED};font-size:11px;font-weight:600;letter-spacing:0.08em;text-transform:uppercase")
        lay.addWidget(hdr)
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"border:none;border-top:1px solid {BORDER};margin:0")
        sep.setFixedHeight(1)
        lay.addWidget(sep)
        return card, lay

    def _settings_row(self, label, widget):
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:120px")
        INPUT_STYLE = (
            f"background:{BG};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:13px;padding:3px 8px"
        )
        widget.setStyleSheet(INPUT_STYLE)
        widget.setFixedHeight(30)
        row.addWidget(lbl)
        row.addWidget(widget, 1)
        return row

    # ── Full UI build ────────────────────────

    def _build_ui(self):
        self.setStyleSheet(f"QMainWindow,QWidget{{background:{BG};color:{TEXT};font-size:13px}}")

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(14)

        # ── LEFT COLUMN ──────────────────────
        left = QVBoxLayout()
        left.setSpacing(10)

        # Header
        hdr_row = QHBoxLayout()
        title = QLabel("VHS capture")
        title.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:600")
        sub = QLabel("Record tapes · detect blue screens · split clips")
        sub.setStyleSheet(f"color:{MUTED};font-size:12px")
        hdr_col = QVBoxLayout()
        hdr_col.setSpacing(2)
        hdr_col.addWidget(title)
        hdr_col.addWidget(sub)
        hdr_row.addLayout(hdr_col)
        hdr_row.addStretch()
        left.addLayout(hdr_row)

        # Video preview card
        preview_card = _card()
        preview_card.setStyleSheet(
            f"QFrame{{background:#000;border:1px solid {BORDER};border-radius:10px;}}"
        )
        preview_stack = QVBoxLayout(preview_card)
        preview_stack.setContentsMargins(0, 0, 0, 0)
        preview_stack.setSpacing(0)

        # The actual preview area (relative positioning via overlay widget)
        self.preview_label = QLabel()
        self.preview_label.setFixedHeight(360)
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background:#000;border-radius:10px")
        self.preview_label.setText(f'<span style="color:{MUTED};font-size:14px">No camera — click Open camera</span>')
        preview_stack.addWidget(self.preview_label)

        # Badge overlay row (inside preview bottom)
        overlay_row = QHBoxLayout()
        overlay_row.setContentsMargins(10, 0, 10, 10)
        overlay_row.setSpacing(6)
        self.badge_idle  = _pill("● Idle",     MUTED, "#1a1a1a")
        self.badge_rec   = _pill("● REC",      "#fff", RED)
        self.badge_blue  = _pill("◆ Blue screen", "#fff", BLUE)
        self.badge_rec.setVisible(False)
        self.badge_blue.setVisible(False)
        overlay_row.addWidget(self.badge_idle)
        overlay_row.addWidget(self.badge_rec)
        overlay_row.addWidget(self.badge_blue)
        overlay_row.addStretch()
        self.lbl_timecode = QLabel("0:00:00")
        self.lbl_timecode.setStyleSheet(
            f"color:{TEXT};background:#1a1a1a;border:1px solid {BORDER};"
            f"border-radius:10px;padding:2px 10px;font-size:11px;font-family:monospace;font-weight:600"
        )
        overlay_row.addWidget(self.lbl_timecode)
        preview_stack.addLayout(overlay_row)
        left.addWidget(preview_card)

        # Blue ratio meter + audio meter row
        meters_row = QHBoxLayout()
        meters_row.setSpacing(8)
        blue_lbl = QLabel("Blue:")
        blue_lbl.setStyleSheet(f"color:{MUTED};font-size:12px;min-width:32px")
        self.blue_bar = QProgressBar()
        self.blue_bar.setRange(0, 100)
        self.blue_bar.setValue(0)
        self.blue_bar.setTextVisible(False)
        self.blue_bar.setFixedHeight(6)
        self.blue_bar.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border-radius:3px;border:none}}"
            f"QProgressBar::chunk{{background:{BLUE};border-radius:3px}}"
        )
        audio_lbl = QLabel("Audio:")
        audio_lbl.setStyleSheet(f"color:{MUTED};font-size:12px;min-width:38px")
        self.audio_bar = QProgressBar()
        self.audio_bar.setRange(0, 100)
        self.audio_bar.setValue(0)
        self.audio_bar.setTextVisible(False)
        self.audio_bar.setFixedHeight(6)
        self.audio_bar.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border-radius:3px;border:none}}"
            f"QProgressBar::chunk{{background:{GREEN};border-radius:3px}}"
        )
        meters_row.addWidget(blue_lbl)
        meters_row.addWidget(self.blue_bar, 1)
        meters_row.addSpacing(12)
        meters_row.addWidget(audio_lbl)
        meters_row.addWidget(self.audio_bar, 1)
        left.addLayout(meters_row)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_cam   = self._make_btn("⏵  Open camera", primary=True)
        self.btn_rec   = self._make_btn("⏺  Start recording", primary=True)
        self.btn_split = self._make_btn("✂  Split now")
        self.btn_cam.clicked.connect(self._open_camera)
        self.btn_rec.clicked.connect(self._toggle_record)
        self.btn_split.clicked.connect(self._manual_split)
        self.btn_rec.setEnabled(False)
        self.btn_split.setEnabled(False)
        btn_row.addWidget(self.btn_cam)
        btn_row.addWidget(self.btn_rec)
        btn_row.addWidget(self.btn_split)
        left.addLayout(btn_row)

        # Stats cards row
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        for attr, title_txt, sub_txt in [
            ("stat_time",   "0:00:00",  "recording time"),
            ("stat_clips",  "0",        "clips saved"),
            ("stat_splits", "0",        "blue screen splits"),
        ]:
            card = _card()
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 12, 14, 12)
            lay.setSpacing(2)
            val = QLabel(title_txt)
            val.setStyleSheet(f"color:{TEXT};font-size:22px;font-weight:600")
            sub = QLabel(sub_txt)
            sub.setStyleSheet(f"color:{MUTED};font-size:11px")
            lay.addWidget(val)
            lay.addWidget(sub)
            setattr(self, attr, val)
            stat_row.addWidget(card, 1)
        left.addLayout(stat_row)
        left.addStretch()

        root.addLayout(left, 3)

        # ── RIGHT COLUMN ─────────────────────
        right = QVBoxLayout()
        right.setSpacing(10)

        # Output dir + filename prefix
        out_card, out_lay = self._section("OUTPUT")
        dir_row = QHBoxLayout()
        self.lbl_outdir = QLabel(self._output_dir)
        self.lbl_outdir.setStyleSheet(f"color:{TEXT};font-size:12px")
        self.lbl_outdir.setWordWrap(True)
        btn_dir = self._make_btn("Browse…")
        btn_dir.setFixedWidth(76)
        btn_dir.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.lbl_outdir, 1)
        dir_row.addWidget(btn_dir)
        out_lay.addLayout(dir_row)

        FIELD_STYLE = (
            f"background:{BG};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:13px;padding:3px 8px"
        )
        self.edit_title = QLineEdit()
        self.edit_title.setPlaceholderText("e.g. Summer1994")
        self.edit_title.setStyleSheet(FIELD_STYLE)
        self.edit_title.setFixedHeight(30)
        out_lay.addLayout(self._settings_row("Title prefix:", self.edit_title))

        self.edit_part = QLineEdit()
        self.edit_part.setPlaceholderText("e.g. TapeA  or  Side1")
        self.edit_part.setStyleSheet(FIELD_STYLE)
        self.edit_part.setFixedHeight(30)
        out_lay.addLayout(self._settings_row("Part prefix:", self.edit_part))

        preview_lbl = QLabel("→  Title_Part_clip01_20240101_120000.mp4")
        preview_lbl.setStyleSheet(f"color:{MUTED};font-size:11px;font-style:italic")
        preview_lbl.setObjectName("fname_preview")
        out_lay.addWidget(preview_lbl)

        def _update_preview():
            t = self.edit_title.text().strip()
            p = self.edit_part.text().strip()
            parts = [x for x in [t, p, "clip01", "20240101_120000"] if x]
            preview_lbl.setText("→  " + "_".join(parts) + ".mp4")
        self.edit_title.textChanged.connect(_update_preview)
        self.edit_part.textChanged.connect(_update_preview)

        right.addWidget(out_card)

        # Capture settings
        cap_card, cap_lay = self._section("CAPTURE SETTINGS")

        self.combo_res = QComboBox()
        self.combo_res.addItems(["1920×1080", "1280×720", "640×480"])
        self.combo_res.setCurrentIndex(1)
        cap_lay.addLayout(self._settings_row("Resolution:", self.combo_res))

        self.spin_fps = QSpinBox(); self.spin_fps.setRange(10, 60); self.spin_fps.setValue(25)
        cap_lay.addLayout(self._settings_row("FPS:", self.spin_fps))

        self.combo_enc = QComboBox()
        self.combo_enc.addItem("Scanning encoders…")
        self.combo_enc.setEnabled(False)
        cap_lay.addLayout(self._settings_row("Encoder:", self.combo_enc))
        self._encoder_list: list[dict] = []
        # Probe encoders in background so the window opens instantly
        def _probe_bg():
            working, failed = probe_encoders()
            self._encoder_list = working
            from PyQt6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self._populate_encoders(working, failed))
        threading.Thread(target=_probe_bg, daemon=True).start()
        right.addWidget(cap_card)

        # Quality slider with named presets
        crf_card, crf_lay = self._section("QUALITY")
        crf_row, self.sl_crf, _ = self._make_slider_row(
            "CRF", 15, 45, 28,
            lambda v: f"{v}  {'Archive' if v<=18 else 'High' if v<=24 else 'Balanced' if v<=30 else 'Preview'}"
        )
        crf_lay.addLayout(crf_row)
        crf_hint = QLabel("15=Archive  24=High  28=Balanced  38=Preview")
        crf_hint.setStyleSheet(f"color:{MUTED};font-size:11px")
        crf_lay.addWidget(crf_hint)
        right.addWidget(crf_card)

        # Blue detection
        det_card, det_lay = self._section("BLUE SCREEN DETECTION")
        thresh_row, self.sl_thresh, _ = self._make_slider_row(
            "Blue threshold", 20, 90, 60, lambda v: f"{v}%")
        det_lay.addLayout(thresh_row)

        dur_row, self.sl_mindur, _ = self._make_slider_row(
            "Min blue duration", 3, 90, 15, lambda v: f"{v/10:.1f}s")
        det_lay.addLayout(dur_row)

        self.chk_auto_split = QCheckBox("Auto-split clip on blue screen")
        self.chk_auto_split.setChecked(True)
        self.chk_auto_split.setStyleSheet(f"color:{MUTED};font-size:13px")
        det_lay.addWidget(self.chk_auto_split)
        right.addWidget(det_card)

        # Clips list
        clips_card, clips_lay = self._section("SAVED CLIPS")
        self.clips_list = QListWidget()
        self.clips_list.setStyleSheet(
            f"QListWidget{{background:{BG};border:none;color:{TEXT};font-size:12px}}"
            f"QListWidget::item{{padding:5px 4px;border-bottom:1px solid {BORDER}}}"
            f"QListWidget::item:selected{{background:{BLUE_BG};color:#fff}}"
        )
        self.clips_list.setMinimumHeight(80)
        self.clips_list.setMaximumHeight(130)
        self.clips_list.itemDoubleClicked.connect(self._open_clip_folder)
        clips_lay.addWidget(self.clips_list)
        clips_hint = QLabel("Double-click to open folder")
        clips_hint.setStyleSheet(f"color:{MUTED};font-size:11px")
        clips_lay.addWidget(clips_hint)
        right.addWidget(clips_card)

        # Event log
        log_card, log_lay = self._section("EVENT LOG")
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(100)
        self.log_view.setStyleSheet(
            f"background:{BG};color:{TEXT};font-family:monospace;font-size:12px;"
            f"border:none;border-radius:0"
        )
        log_lay.addWidget(self.log_view)
        right.addWidget(log_card, 1)

        root.addLayout(right, 2)

    # ── Settings persistence ──────────────────

    def _populate_encoders(self, working: list[dict], failed: list[dict] = None):
        failed = failed or []
        self.combo_enc.clear()
        if not working:
            self.combo_enc.addItem("No encoders found — check FFmpeg install")
        else:
            for e in working:
                self.combo_enc.addItem(e["label"])
            self.combo_enc.setEnabled(True)

        # Restore saved encoder preference
        saved = load_settings().get("encoder", "")
        restored = False
        for i, e in enumerate(working):
            if e["name"] == saved:
                self.combo_enc.setCurrentIndex(i)
                restored = True
                break

        # Auto-select best hw encoder if no saved preference
        if not restored:
            hw = [e for e in working if e["is_hw"]]
            if hw:
                self.combo_enc.setCurrentIndex(working.index(hw[0]))

        # Log working encoders
        hw_working = [e for e in working if e["is_hw"]]
        sw_working = [e for e in working if not e["is_hw"]]
        if hw_working:
            self._log_event("info",
                f"GPU encoders ready: {', '.join(e['name'] for e in hw_working)}", "")
        else:
            self._log_event("info", "No GPU encoders available", "using CPU encoding")
        if sw_working:
            self._log_event("info",
                f"CPU encoders: {', '.join(e['name'] for e in sw_working)}", "")

        # Log why each hw encoder failed
        for e in failed:
            self._log_event("stop",
                f"{e['name']} unavailable", e.get("fail_reason", ""))

    def _restore_settings(self, cfg: dict):
        self.lbl_outdir.setText(self._output_dir)
        self.edit_title.setText(cfg.get("title_prefix", ""))
        self.edit_part.setText(cfg.get("part_prefix", ""))
        # Encoder restored in _populate_encoders once the probe finishes
        res_map = {"1920x1080": 0, "1280x720": 1, "640x480": 2}
        self.combo_res.setCurrentIndex(res_map.get(cfg.get("resolution", "1280x720"), 1))
        self.spin_fps.setValue(cfg.get("fps", 25))
        self.sl_crf.setValue(cfg.get("crf", 28))
        self.sl_thresh.setValue(cfg.get("blue_threshold_pct", 60))
        self.sl_mindur.setValue(cfg.get("min_blue_dur", 15))
        self.chk_auto_split.setChecked(cfg.get("auto_split", True))

    def _selected_encoder_info(self) -> dict | None:
        idx = self.combo_enc.currentIndex()
        if self._encoder_list and 0 <= idx < len(self._encoder_list):
            return self._encoder_list[idx]
        return None

    def _current_settings(self) -> dict:
        res_text = self.combo_res.currentText().replace("×", "x")
        info = self._selected_encoder_info()
        return {
            "output_dir":         self._output_dir,
            "title_prefix":       self.edit_title.text().strip(),
            "part_prefix":        self.edit_part.text().strip(),
            "encoder":            info["name"] if info else "libsvtav1",
            "resolution":         res_text,
            "fps":                self.spin_fps.value(),
            "crf":                self.sl_crf.value(),
            "blue_threshold_pct": self.sl_thresh.value(),
            "min_blue_dur":       self.sl_mindur.value(),
            "auto_split":         self.chk_auto_split.isChecked(),
        }

    def _save_settings(self):
        save_settings(self._current_settings())

    # ── Windows dark/light theme ──────────────

    def _apply_windows_theme(self):
        """Follow the Windows system dark/light preference if on Windows."""
        if sys.platform != "win32":
            return
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            )
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            is_light = val == 1
        except Exception:
            return  # can't read registry, stay dark

        if is_light:
            # Switch to a lighter variant while keeping the same design language
            app = QApplication.instance()
            p = app.palette()
            p.setColor(QPalette.ColorRole.Window,        QColor(245, 245, 245))
            p.setColor(QPalette.ColorRole.WindowText,    QColor(30, 30, 30))
            p.setColor(QPalette.ColorRole.Base,          QColor(255, 255, 255))
            p.setColor(QPalette.ColorRole.Text,          QColor(30, 30, 30))
            p.setColor(QPalette.ColorRole.Button,        QColor(235, 235, 235))
            p.setColor(QPalette.ColorRole.ButtonText,    QColor(30, 30, 30))
            app.setPalette(p)

    # ── Camera ───────────────────────────────

    def _open_camera(self):
        dlg = CameraPickerDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        if self._capture:
            self._capture.stop()
            self._capture = None

        self._camera_index   = dlg.selected_index
        self._camera_native_w = dlg.selected_w
        self._camera_native_h = dlg.selected_h
        self._camera_native_fps = dlg.selected_fps

        # Update resolution combo to match camera's native res
        res_str = f"{dlg.selected_w}×{dlg.selected_h}"
        idx = self.combo_res.findText(res_str)
        if idx >= 0:
            self.combo_res.setCurrentIndex(idx)
        if 10 <= dlg.selected_fps <= 60:
            self.spin_fps.setValue(int(round(dlg.selected_fps)))

        cfg = self._get_config()
        self._capture = CaptureThread(cfg)
        self._capture.frame_ready.connect(self._on_frame)
        self._capture.blue_changed.connect(self._on_blue_changed)
        self._capture.segment_split.connect(self._on_segment_split)
        self._capture.timecode_tick.connect(self._on_timecode)
        self._capture.error.connect(self._on_error)
        self._capture.start()

        # Start audio meter
        if self._audio_meter:
            self._audio_meter.stop()
        if _AUDIO_AVAILABLE:
            self._audio_meter = AudioMeterThread()
            self._audio_meter.level.connect(
                lambda v: self.audio_bar.setValue(int(v * 100))
            )
            self._audio_meter.start()

        self.btn_rec.setEnabled(True)
        self.btn_cam.setText("⏵  Change camera")
        self.badge_idle.setText("● Live")
        self._log_event("info", "Camera opened",
                        f"index {cfg['camera_index']} @ {cfg['width']}×{cfg['height']} {cfg['fps']}fps")

    def _get_config(self):
        res_map = {"1920×1080": (1920, 1080), "1280×720": (1280, 720), "640×480": (640, 480)}
        combo_text = self.combo_res.currentText()
        if combo_text in res_map:
            w, h = res_map[combo_text]
        else:
            w, h = self._camera_native_w, self._camera_native_h
        info = self._selected_encoder_info()
        enc  = info["name"] if info else "libsvtav1"
        return {
            "camera_index":     self._camera_index,
            "width": w, "height": h,
            "fps":              self.spin_fps.value(),
            "encoder":          enc,
            "encoder_info":     info,
            "crf":              self.sl_crf.value(),
            "blue_threshold":   self.sl_thresh.value() / 100,
            "min_blue_seconds": self.sl_mindur.value() / 10,
            "auto_split":       self.chk_auto_split.isChecked(),
            "output_dir":       self._output_dir,
            "title_prefix":     self.edit_title.text().strip(),
            "part_prefix":      self.edit_part.text().strip(),
        }

    # ── Recording ────────────────────────────

    def _toggle_record(self):
        if not self._recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not self._capture:
            return
        # Refresh the full config so any UI changes since camera open take effect
        self._capture.cfg.update(self._get_config())
        self._capture.start_recording()
        self._recording = True
        self._seg_count = 0
        self._clips = []
        self.stat_clips.setText("0")
        self.stat_splits.setText("0")
        self.btn_rec.setText("⏹  Stop recording")
        self.btn_rec.setStyleSheet(
            f"QPushButton{{background:{RED_BG};color:{RED};border:1px solid {RED};"
            f"border-radius:8px;padding:0 18px;font-size:13px;font-weight:500;height:36px}}"
            f"QPushButton:hover{{background:#3a0e0e}}"
        )
        self.btn_split.setEnabled(True)
        self.badge_idle.setVisible(False)
        self.badge_rec.setVisible(True)
        self._log_event("rec", "Recording started", self._output_dir)

    def _stop_recording(self):
        if self._capture:
            self._capture.stop_recording()
        self._recording = False
        self.btn_rec.setText("⏺  Start recording")
        self.btn_rec.setStyleSheet(
            f"QPushButton{{background:{SURFACE};color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:8px;padding:0 18px;font-size:13px;font-weight:500;height:36px}}"
            f"QPushButton:hover{{background:#252525;border-color:#444}}"
            f"QPushButton:disabled{{color:#444;border-color:{BORDER}}}"
        )
        self.btn_split.setEnabled(False)
        self.badge_rec.setVisible(False)
        self.badge_idle.setVisible(True)
        self.badge_idle.setText("● Idle")
        self._log_event("stop", "Recording stopped",
                        f"{self._seg_count} blue screen splits · {len(self._clips)} clips")

    def _manual_split(self):
        if self._capture and self._recording:
            self._capture.manual_split()
            self._log_event("split", "Manual split", f"at {self.lbl_timecode.text()}")

    # ── Signals ──────────────────────────────

    def _on_frame(self, frame: np.ndarray):
        # frame is already 640×360 BGR from capture thread
        h, w = frame.shape[:2]

        # Draw waveform overlay when recording
        if self._recording:
            frame = frame.copy()
            audio_val = self.audio_bar.value() / 100.0
            bar_h = max(2, int(audio_val * 40))
            bar_w = 8
            x = w - bar_w - 10
            y_top = h - 10 - bar_h
            # Background track
            cv2.rectangle(frame, (x, h - 10 - 40), (x + bar_w, h - 10),
                          (40, 40, 40), -1)
            # Level bar — green, yellow, red by level
            colour = (40, 200, 40) if audio_val < 0.7 else \
                     (40, 200, 200) if audio_val < 0.9 else (40, 40, 220)
            cv2.rectangle(frame, (x, y_top), (x + bar_w, h - 10), colour, -1)
            # REC dot
            cv2.circle(frame, (14, 14), 6, (40, 40, 220), -1)

        if _RUST_AVAILABLE:
            raw = _vhs_core.bgr_to_rgb(frame.tobytes())
            img = QImage(raw, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        else:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img = QImage(rgb.data, w, h, w * 3, QImage.Format.Format_RGB888).copy()
        pix = QPixmap.fromImage(img)
        lw, lh = self.preview_label.width(), self.preview_label.height()
        if pix.width() != lw or pix.height() != lh:
            pix = pix.scaled(lw, lh,
                             Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.FastTransformation)
        self.preview_label.setPixmap(pix)

    def _on_blue_changed(self, is_blue: bool, ratio: float):
        self._in_blue = is_blue
        self.badge_blue.setVisible(is_blue)
        pct = int(ratio * 100)
        self.blue_bar.setValue(pct)
        # Colour the blue bar red when above threshold (triggered)
        if is_blue:
            self.blue_bar.setStyleSheet(
                f"QProgressBar{{background:{BORDER};border-radius:3px;border:none}}"
                f"QProgressBar::chunk{{background:{RED};border-radius:3px}}"
            )
            self._seg_count += 1
            self.stat_splits.setText(str(self._seg_count))
            self._log_event("blue", "Blue screen detected", f"{pct}% blue pixels")
        else:
            self.blue_bar.setStyleSheet(
                f"QProgressBar{{background:{BORDER};border-radius:3px;border:none}}"
                f"QProgressBar::chunk{{background:{BLUE};border-radius:3px}}"
            )
            self._log_event("resume", "Content resumed", "new segment started")

    def _on_segment_split(self, path: str, elapsed: float):
        self._clips.append(path)
        self.stat_clips.setText(str(len(self._clips)))
        name = Path(path).name
        self._log_event("split", f"New segment: {name}", f"t={elapsed:.1f}s")
        # Add to clips list widget
        item = QListWidgetItem(f"  {len(self._clips):02d}  {name}")
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.clips_list.addItem(item)
        self.clips_list.scrollToBottom()

    def _open_clip_folder(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        folder = str(Path(path).parent)
        if sys.platform == "win32":
            # Open Explorer and select the file
            subprocess.Popen(["explorer", "/select,", path.replace("/", "\\")])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _on_timecode(self, elapsed: float):
        td = timedelta(seconds=int(elapsed))
        s = str(td)
        self.lbl_timecode.setText(s)
        self.stat_time.setText(s)

    def _on_error(self, msg: str):
        self._log_event("error", "Error", msg)

    # ── Helpers ──────────────────────────────

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory", self._output_dir)
        if d:
            self._output_dir = d
            self.lbl_outdir.setText(d)

    def _log_event(self, kind: str, msg: str, detail: str = ""):
        self._log.add(kind, msg, detail)
        self.log_view.setHtml(self._log.format_html())
        self.log_view.moveCursor(QTextCursor.MoveOperation.End)

    def closeEvent(self, event):
        self._save_settings()
        if self._audio_meter:
            self._audio_meter.stop()
        if self._capture:
            self._capture.stop()
        event.accept()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window,          QColor(15, 15, 15))
    palette.setColor(QPalette.ColorRole.WindowText,      QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Base,            QColor(10, 10, 10))
    palette.setColor(QPalette.ColorRole.AlternateBase,   QColor(26, 26, 26))
    palette.setColor(QPalette.ColorRole.Text,            QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Button,          QColor(26, 26, 26))
    palette.setColor(QPalette.ColorRole.ButtonText,      QColor(232, 232, 232))
    palette.setColor(QPalette.ColorRole.Highlight,       QColor(55, 138, 221))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(palette)

    win = VHSCapture()
    win.show()
    sys.exit(app.exec())
