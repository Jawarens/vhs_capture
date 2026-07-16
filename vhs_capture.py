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
    QImage, QPixmap, QColor, QPalette, QTextCursor, QPainter, QPen, QIcon
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

# On Windows, suppress the console window that briefly flashes for every
# subprocess. Safe to unpack into both Popen() and run() calls.
_NO_WINDOW = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32" else {}
)

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
        try:
            # Open with a reasonable default blocksize; query actual rate after open
            with sd.InputStream(channels=1, dtype='float32') as stream:
                actual_rate = stream.samplerate or 44100
                block = max(512, int(actual_rate // 20))  # ~50ms blocks
                while self._running:
                    data, _ = stream.read(block)
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    db  = max(-60.0, 20 * np.log10(rms + 1e-9))
                    lvl = (db + 60) / 60
                    self.level.emit(min(1.0, max(0.0, lvl)))
        except Exception:
            pass

    def stop(self):
        self._running = False
        self.quit()
        self.wait(1000)


# ─────────────────────────────────────────────
#  Hardware encoder detection + profiles
# ─────────────────────────────────────────────

def _system_ffmpeg() -> str | None:
    """
    Find a system FFmpeg that supports hw encoders (NVENC/QSV).
    Checks PATH first, then common Windows install locations from
    winget, chocolatey, scoop, and manual installs.
    """
    import shutil as _sh

    # 1. Already on PATH (most common after winget/choco/scoop with shell restart)
    found = _sh.which("ffmpeg") or _sh.which("ffmpeg.exe")
    if found:
        return found

    # 2. Common Windows locations where FFmpeg ends up NOT on PATH
    if sys.platform == "win32":
        import os
        candidates = []

        # winget installs to per-user AppData or ProgramFiles
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        user_profile = os.environ.get("USERPROFILE", "")

        candidates += [
            # winget / Microsoft Store style
            os.path.join(local_app, "Microsoft", "WinGet", "Packages",
                         "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
                         "ffmpeg-7.1-full_build", "bin", "ffmpeg.exe"),
            # Chocolatey
            os.path.join("C:\\", "ProgramData", "chocolatey", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "tools", "ffmpeg", "bin", "ffmpeg.exe"),
            # Scoop
            os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current",
                         "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ProgramData", "scoop", "apps", "ffmpeg",
                         "current", "bin", "ffmpeg.exe"),
            # Manual installs in common spots
            os.path.join(prog_files, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(prog_files_x86, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ffmpeg", "ffmpeg.exe"),
            # Next to our own exe (user may have placed it there)
            os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        ]

        # Also scan winget packages directory for any ffmpeg build
        winget_pkg_dir = os.path.join(local_app, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkg_dir):
            for entry in os.listdir(winget_pkg_dir):
                if "ffmpeg" in entry.lower() or "gyan" in entry.lower():
                    for root, dirs, files in os.walk(
                            os.path.join(winget_pkg_dir, entry)):
                        for f in files:
                            if f.lower() == "ffmpeg.exe":
                                candidates.append(os.path.join(root, f))

        for c in candidates:
            if os.path.isfile(c):
                return c

    return None


# Full encoder profiles. Each entry:
#   name, label, is_hw, pixfmt,
#   requires_hw_ffmpeg — True if imageio-ffmpeg won't have it
#   probe_size         — minimum valid resolution for this encoder
#   args(crf, w, h)    — extra flags after "-c:v <name>"
_ENCODER_PROFILES = [
    # ── NVIDIA NVENC ─────────────────────────────────────────────────
    # NVENC AV1 minimum resolution: 145×145. H264/HEVC: 128×128.
    # constqp = constant QP (like CRF). p4 = balanced. tune=hq for quality.
    # -surfaces 8: pre-allocated encode surfaces to avoid stalls.
    {
        "name": "av1_nvenc", "label": "AV1  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0",
            "-preset", "p4", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "8",
        ],
    },
    {
        "name": "hevc_nvenc", "label": "HEVC  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0",
            "-preset", "p4", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "8",
        ],
    },
    {
        "name": "h264_nvenc", "label": "H.264  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0",
            "-preset", "p4", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "8",
        ],
    },
    # ── Intel QuickSync ──────────────────────────────────────────────
    # Use yuv420p input (QSV handles upload internally, avoids filter errors).
    # global_quality 1-51: lower = better quality.
    # look_ahead_depth 0 = lowest latency for live capture.
    {
        "name": "av1_qsv", "label": "AV1  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    {
        "name": "hevc_qsv", "label": "HEVC  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    {
        "name": "h264_qsv", "label": "H.264  —  Intel QuickSync ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    # ── Software ─────────────────────────────────────────────────────
    # -threads 0 = use all CPU cores for FFmpeg overhead.
    # SVT-AV1 manages its own thread pool via -preset.
    # libaom -row-mt 1 enables row-based multi-threading.
    # x265 pools=+ uses all cores; frame-threads=4 for parallel frame encoding.
    {
        "name": "libsvtav1", "label": "AV1  —  SVT-AV1 (CPU fast)",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False, "probe_size": "64x64",
        "args": lambda q, w, h: ["-preset", "6", "-crf", str(q), "-threads", "0"],
    },
    {
        "name": "libaom-av1", "label": "AV1  —  libaom (CPU best quality)",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False, "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-cpu-used", "6", "-b:v", "0", "-crf", str(q),
            "-threads", "0", "-row-mt", "1",
        ],
    },
    {
        "name": "libx265", "label": "HEVC / H.265  —  CPU",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False, "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-preset", "fast", "-crf", str(q),
            "-threads", "0", "-x265-params", "pools=+:frame-threads=4",
        ],
    },
    {
        "name": "libx264", "label": "H.264  —  CPU",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False, "probe_size": "64x64",
        "args": lambda q, w, h: ["-preset", "fast", "-crf", str(q), "-threads", "0"],
    },
]


def _friendly_hw_error(enc: str, stderr: str) -> str:
    s = stderr.lower()
    is_qsv   = "qsv" in enc
    is_nvenc = "nvenc" in enc

    # ── QSV-specific ─────────────────────────────────────────────────
    if is_qsv:
        if "invalid argument" in s or "error code: -22" in s:
            return (f"{enc}: Intel MFX/OneVPL runtime not installed. "
                    "Install 'Intel Graphics Driver' from intel.com/graphics "
                    "AND 'Intel oneVPL GPU Runtime' from github.com/intel/libvpl")
        if "mfx session" in s or "mfxinit" in s or "error creating" in s:
            return (f"{enc}: MFX session failed — install Intel oneVPL GPU Runtime: "
                    "github.com/intel/libvpl/releases")
        if "unsupported" in s or "not support" in s:
            return f"{enc}: Intel GPU too old for this codec (requires 6th gen Intel Core or newer)"
        if "no capable devices" in s or "device open failed" in s:
            return f"{enc}: no Intel GPU found or driver not loaded"

    # ── NVENC-specific ───────────────────────────────────────────────
    if is_nvenc:
        if "invalid argument" in s or "error code: -22" in s:
            return (f"{enc}: invalid argument — driver mismatch or codec not supported "
                    "(AV1 NVENC requires RTX 3000+; ensure Game Ready/Studio driver is current)")
        if "libcuda" in s or "cannot load nvcuda" in s or "no cuda" in s:
            return f"{enc}: NVIDIA driver not found — install Game Ready or Studio driver"
        if "no capable devices" in s or "device open failed" in s:
            return f"{enc}: no capable NVIDIA GPU found"
        if "not enough" in s and "surface" in s:
            return f"{enc}: not enough VRAM surfaces — try a lower resolution"

    # ── Generic ──────────────────────────────────────────────────────
    if "unsupported" in s or "not support" in s:
        return f"{enc}: GPU too old or codec not supported"
    for line in reversed(stderr.strip().splitlines()):
        line = line.strip()
        if line and ("error" in line.lower() or "fail" in line.lower()):
            return line[:140]
    return f"{enc}: probe failed (unknown error)"


def probe_encoders():
    """
    Test each encoder by piping real rawvideo at its minimum valid resolution.
    All probes run in parallel via ThreadPoolExecutor.
    Returns (working: list[dict], failed_hw: list[dict]).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    sys_ff = _system_ffmpeg()
    bnd_ff = FFMPEG_BIN

    def _test(profile):
        enc       = profile["name"]
        pixfmt    = profile["pixfmt"]
        psize     = profile.get("probe_size", "256x256")
        pw, ph    = (int(x) for x in psize.split("x"))
        args      = profile["args"](28, pw, ph)
        needs_sys = profile.get("requires_hw_ffmpeg", False)
        ff = sys_ff if needs_sys else bnd_ff

        if ff is None:
            if profile["is_hw"]:
                return "failed", {**profile,
                    "fail_reason": f"{enc}: system FFmpeg not found — run: winget install ffmpeg"}
            return "skip", None

        black = bytes(pw * ph * 3)
        cmd = [
            ff, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", psize, "-r", "25", "-i", "pipe:0",
            "-c:v", enc,
        ] + args + ["-pix_fmt", pixfmt, "-frames:v", "2", "-f", "null", "-"]

        try:
            r = subprocess.run(cmd, input=black, capture_output=True, timeout=15,
                               **_NO_WINDOW)
            if r.returncode == 0:
                p = dict(profile)
                p["ffmpeg_bin"] = ff
                return "ok", p
            elif profile["is_hw"]:
                stderr = r.stderr.decode(errors="replace")
                return "failed", {**profile,
                    "fail_reason": _friendly_hw_error(enc, stderr),
                    "stderr_raw": stderr[-500:]}
            return "skip", None
        except subprocess.TimeoutExpired:
            if profile["is_hw"]:
                return "failed", {**profile, "fail_reason": f"{enc}: probe timed out"}
            return "skip", None
        except FileNotFoundError:
            return "skip", None

    results = [None] * len(_ENCODER_PROFILES)
    with ThreadPoolExecutor(max_workers=len(_ENCODER_PROFILES)) as ex:
        futures = {ex.submit(_test, p): i for i, p in enumerate(_ENCODER_PROFILES)}
        for fut in as_completed(futures):
            results[futures[fut]] = fut.result()

    working, failed = [], []
    for item in results:
        if item is None:
            continue
        status, data = item
        if status == "ok":
            working.append(data)
        elif status == "failed" and data:
            failed.append(data)
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
        ff      = info.get("ffmpeg_bin", FFMPEG_BIN)

        args_fn  = info.get("args")
        enc_args = args_fn(self.crf, self.width, self.height) if args_fn \
                   else ["-crf", str(self.crf), "-threads", "0"]

        cmd = [
            ff, "-y",
            # Increase probe size / thread queues to handle bursty frame delivery
            "-thread_queue_size", "512",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
            "-c:v", enc,
        ] + enc_args + ["-pix_fmt", pixfmt]
        cmd += ["-movflags", "+faststart", self.output_path]

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,   # capture for error reporting
            **_NO_WINDOW
        )
        # Drain stderr in background so it never blocks
        self._stderr_lines: list[str] = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        if self._proc and self._proc.stderr:
            for line in self._proc.stderr:
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                # Skip normal FFmpeg progress lines — not errors
                if decoded.startswith("frame=") or decoded.startswith("size="):
                    continue
                if decoded.startswith("video:") or decoded.startswith("audio:"):
                    continue
                self._stderr_lines.append(decoded)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines = self._stderr_lines[-100:]

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
                try:
                    self._proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    self._proc.terminate()     # polite kill
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()      # force kill
                self._proc = None

    @property
    def running(self):
        return self._proc is not None


# ─────────────────────────────────────────────
#  Audio device enumeration
# ─────────────────────────────────────────────

def list_audio_devices(ffmpeg_bin: str) -> list[str]:
    """
    Enumerate audio input devices available to FFmpeg.
    On Windows uses DirectShow (-f dshow -list_devices).
    Falls back to sounddevice if FFmpeg listing fails.
    """
    devices = []

    if sys.platform == "win32":
        # FFmpeg dshow device listing — use a strict timeout, it can hang
        cmd = [ffmpeg_bin, "-list_devices", "true",
               "-f", "dshow", "-i", "dummy",
               "-loglevel", "debug"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=6, **_NO_WINDOW)
            # Output is on stderr for dshow
            output = r.stderr.decode(errors="replace")

            import re
            # dshow lists audio devices in a block after the audio header.
            # Different FFmpeg versions format this slightly differently.
            # Strategy 1: look for lines with "(audio)" marker
            for line in output.splitlines():
                if "(audio)" in line.lower():
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        name = m.group(1)
                        if name not in devices and name != "dummy":
                            devices.append(name)

            # Strategy 2: parse the structured block
            # [dshow @ ...] DirectShow audio devices
            # [dshow @ ...]  "Device Name"
            if not devices:
                in_audio_section = False
                for line in output.splitlines():
                    if "DirectShow audio devices" in line:
                        in_audio_section = True
                        continue
                    if in_audio_section:
                        if "DirectShow video devices" in line:
                            break
                        m = re.search(r'"([^"]+)"', line)
                        if m:
                            name = m.group(1)
                            if name not in devices:
                                devices.append(name)
        except Exception:
            pass

    else:
        # Linux: try pactl
        try:
            r = subprocess.run(["pactl", "list", "sources", "short"],
                               capture_output=True, timeout=5, **_NO_WINDOW)
            for line in r.stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    devices.append(parts[1])
        except Exception:
            pass

    # Fallback: sounddevice
    if not devices and _AUDIO_AVAILABLE:
        try:
            import sounddevice as _sd
            for d in _sd.query_devices():
                if d["max_input_channels"] > 0:
                    devices.append(d["name"])
        except Exception:
            pass

    return devices


# ─────────────────────────────────────────────
#  Per-segment audio recorder
# ─────────────────────────────────────────────

class AudioRecorder:
    """
    Captures audio from a named device into a temp WAV file using FFmpeg.
    One instance per video segment. When stop() is called, the WAV is
    muxed into the video file (replacing it) in a background thread.
    """

    def __init__(self, device_name: str, temp_path: str, ffmpeg_bin: str):
        self.device_name = device_name
        self.temp_path   = temp_path   # e.g. /tmp/vhs_audio_001.wav
        self.ffmpeg_bin  = ffmpeg_bin
        self._proc: subprocess.Popen | None = None

    def start(self):
        if sys.platform == "win32":
            # FFmpeg dshow requires device names in UTF-8 encoding
            # Handle special characters properly by ensuring proper encoding
            device_name = self.device_name
            # Escape any special characters that might interfere with FFmpeg parsing
            # FFmpeg expects the device name exactly as shown in -list_devices
            device_arg = f"audio={device_name}"
            print(f"[AudioRecorder] Starting dshow capture: {device_arg}")
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "dshow",
                "-analyzeduration", "4000000",  # 4s for device probing
                "-probesize", "4000000",
                "-i", device_arg,
                "-f", "s16le", # raw PCM — no header to corrupt on hard kill
                "-ar", "48000",
                "-ac", "2",
                "-thread_queue_size", "2048",
                self.temp_path, # .pcm extension
            ]
        else:
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "pulse",
                "-i", self.device_name,
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                self.temp_path,
            ]
        # No stdin=PIPE — dshow on Windows ignores stdin 'q'.
        # We stop with terminate(); raw PCM has no header to corrupt.
        print(f"[AudioRecorder] Starting FFmpeg: {' '.join(cmd[:10])}...")
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, # capture for error reporting
            **_NO_WINDOW
        )
        # Drain stderr and log any errors
        def _drain_stderr():
            if self._proc and self._proc.stderr:
                for line in self._proc.stderr:
                    decoded = line.decode(errors="replace").strip()
                    if decoded:
                        # Look for actual errors
                        if any(err in decoded.lower() for err in ["error", "fail", "cannot", "invalid"]):
                            print(f"[AudioRecorder STDERR] {decoded}")
        threading.Thread(target=_drain_stderr, daemon=True, name="audio-stderr").start()

    def stop_and_mux(self, video_path: str, on_done=None, on_error=None):
        """
        Stop audio capture, then mux audio+video into the final file.
        Runs in a background thread so it never blocks the capture loop.
        video_path is the completed video-only file — replaced by muxed output.
        """
        def _work():
            # Stop audio capture — hard-kill is fine for raw PCM (no header to corrupt)
            if self._proc:
                try:
                    self._proc.terminate()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2)
                self._proc = None

            # Give the OS a moment to flush file buffers to disk
            time.sleep(0.5)

            # Check PCM file exists and has content
            # Raw s16le: 48000 samples * 2 ch * 2 bytes = 192000 bytes/sec
            pcm = Path(self.temp_path)
            if not pcm.exists() or pcm.stat().st_size < 20480:
                if on_error:
                    on_error("Audio file empty or missing — saved video only")
                pcm.unlink(missing_ok=True)
                return

            # Mux: video + audio → temp output, then replace original
            vpath   = Path(video_path)
            out_tmp = vpath.with_suffix(".mux_tmp" + vpath.suffix)
            # -movflags +faststart is MP4-only; skip for MKV
            is_mp4  = vpath.suffix.lower() == ".mp4"
            cmd = [
                self.ffmpeg_bin, "-y",
                "-i", video_path,
                # Read raw PCM with explicit format/rate/channels
                "-f", "s16le", "-ar", "48000", "-ac", "2",
                "-i", str(pcm),
                "-c:v", "copy",        # video stream: zero re-encode
                "-c:a", "aac",
                "-b:a", "192k",
                "-shortest",           # trim to shorter of video/audio
            ]
            if is_mp4:
                cmd += ["-movflags", "+faststart"]
            cmd.append(str(out_tmp))
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=180, **_NO_WINDOW)
                if r.returncode == 0 and out_tmp.exists():
                    out_tmp.replace(vpath)   # atomic replace
                    if on_done:
                        on_done()
                else:
                    err = r.stderr.decode(errors="replace")[-400:]
                    if on_error:
                        on_error(f"Mux failed (rc={r.returncode}): {err}")
                    out_tmp.unlink(missing_ok=True)
            except Exception as e:
                if on_error:
                    on_error(str(e))
                out_tmp.unlink(missing_ok=True)
            finally:
                pcm.unlink(missing_ok=True)

        threading.Thread(target=_work, daemon=True).start()



# ─────────────────────────────────────────────
#  Capture thread
# ─────────────────────────────────────────────

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
        self._in_blue = False
        self._last_audio_val = 0.0   # updated by UI thread via set_audio_level()
        self._mutex = QMutex()
        self._writer_thread = _WriterThread(
            on_error=lambda errs: self.error.emit("FFmpeg: " + errs[-1])
        )
        self._writer_thread.start()
        self._audio_recorder: AudioRecorder | None = None
        self._current_video_path: str = ""

    def start_recording(self):
        with QMutexLocker(self._mutex):
            self._segment_index = 0
            self._start_time = time.time()
            self._recording = True
            self._start_new_segment()

    def stop_recording(self):
        with QMutexLocker(self._mutex):
            self._recording = False
        # Clean up audio recorder first to ensure orderly shutdown
        if self._audio_recorder:
            prev_audio, self._audio_recorder = self._audio_recorder, None
            prev_audio.stop_and_mux(
                self._current_video_path,
                on_done=lambda:None,  # already signaling on stop
                on_error=self.error.emit,
            )
        self._writer_thread.set_writer(None)
        self._finish_audio_segment(self._current_video_path)

    def manual_split(self):
        # Do NOT hold the mutex while calling _start_new_segment —
        # it emits a signal which can reenter from the UI thread.
        with QMutexLocker(self._mutex):
            if not self._recording:
                return
        self._start_new_segment()

    def set_audio_level(self, val: float):
        """Called from UI thread; float read in capture thread is atomic on CPython."""
        self._last_audio_val = val

    def _finish_audio_segment(self, video_path: str):
        """Stop current audio recorder and mux into the video file."""
        ar = self._audio_recorder
        self._audio_recorder = None
        if ar and video_path:
            ar.stop_and_mux(
                video_path,
                on_done=lambda: self.segment_split.emit(video_path + ":muxed", 0.0),
                on_error=lambda e: self.error.emit(f"Audio mux: {e}"),
            )

    def _start_new_segment(self):
        cfg = self.cfg
        out_dir = Path(cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _MP4_ENCODERS = {
            "libsvtav1", "libaom-av1", "libx265", "libx264",
            "av1_nvenc", "hevc_nvenc", "h264_nvenc",
            "av1_qsv",   "hevc_qsv",   "h264_qsv",
        }
        ext = "mp4" if cfg["encoder"] in _MP4_ENCODERS else "mkv"
        title  = cfg.get("title_prefix", "").strip()
        part   = cfg.get("part_prefix", "").strip()
        clip_n = f"clip{self._segment_index+1:02d}"
        parts  = [p for p in [title, part, clip_n, ts] if p]
        fname  = out_dir / f"{'_'.join(parts)}.{ext}"
        self._segment_index += 1

        # Finish previous audio segment before starting writer for new one
        prev_video = self._current_video_path
        self._current_video_path = str(fname)
        self._finish_audio_segment(prev_video)

        # Start video writer
        w = FFmpegWriter(
            str(fname),
            cfg["width"], cfg["height"], cfg["fps"],
            encoder=cfg["encoder"], crf=cfg["crf"],
            encoder_info=cfg.get("encoder_info"),
        )
        w.start()
        self._writer_thread.set_writer(w)

        # Start audio recorder if configured
        audio_dev = cfg.get("audio_device", "")
        ff = cfg.get("encoder_info", {}).get("ffmpeg_bin", FFMPEG_BIN) or FFMPEG_BIN
        sys_ff = _system_ffmpeg() or FFMPEG_BIN
        if audio_dev:
            tmp_pcm = str(out_dir / f".audio_tmp_{self._segment_index:03d}.pcm")
            ar = AudioRecorder(audio_dev, tmp_pcm, sys_ff)
            ar.start()
            self._audio_recorder = ar

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

        # Snapshot all hot-loop config values so the tight loop never touches
        # the shared cfg dict (which the UI thread can update via cfg.update()).
        # Re-snapshot at each new segment start via _refresh_loop_cfg().
        def _snapshot_cfg():
            return {
                "blue_threshold":  cfg["blue_threshold"],
                "min_blue_secs":   cfg["min_blue_seconds"],
                "auto_split":      cfg.get("auto_split", True),
            }
        loop = _snapshot_cfg()

        # Time-based blue detection state (seconds, not frames)
        blue_start_time: float | None = None
        resume_start_time: float | None = None
        HYSTERESIS_SECS = 0.5

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
                                     interpolation=cv2.INTER_NEAREST)
                # Draw overlay on capture thread so UI thread just blits
                if self._recording:
                    audio_val = self._last_audio_val
                    bar_h = max(2, int(audio_val * 40))
                    bar_w = 8
                    pw, ph = 640, 360
                    x = pw - bar_w - 10
                    y_bot = ph - 10
                    cv2.rectangle(preview, (x, y_bot - 40), (x + bar_w, y_bot),
                                  (40, 40, 40), -1)
                    colour = (0, 200, 40) if audio_val < 0.7 else \
                             (0, 200, 200) if audio_val < 0.9 else (0, 40, 220)
                    cv2.rectangle(preview, (x, y_bot - bar_h), (x + bar_w, y_bot),
                                  colour, -1)
                    cv2.circle(preview, (14, 14), 6, (0, 0, 200), -1)  # REC dot BGR=red
                self.frame_ready.emit(preview)

            # ── Blue detection (every Nth frame) ────────────
            if frame_count % _DETECT_EVERY_N == 0:
                ratio       = analyse_blue_ratio(frame)
                is_blue_now = ratio >= loop["blue_threshold"]
                auto_split  = loop["auto_split"]
                min_blue    = loop["min_blue_secs"]

                if is_blue_now:
                    resume_start_time = None
                    if blue_start_time is None:
                        blue_start_time = now

                    if now - blue_start_time >= min_blue and not self._in_blue:
                        self._in_blue = True
                        self.blue_changed.emit(True, ratio)
                        if auto_split:
                            with QMutexLocker(self._mutex):
                                if self._recording:
                                    self._writer_thread.set_writer(None)
                else:
                    blue_start_time = None

                    if self._in_blue:
                        if resume_start_time is None:
                            resume_start_time = now
                        elif now - resume_start_time >= HYSTERESIS_SECS:
                            self._in_blue = False
                            resume_start_time = None
                            self.blue_changed.emit(False, ratio)
                            if auto_split:
                                with QMutexLocker(self._mutex):
                                    if self._recording:
                                        self._start_new_segment()
                                # Re-snapshot cfg in case encoder/quality changed
                                loop = _snapshot_cfg()

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

class _AudioScanner(QThread):
    """Scans for audio devices in a background thread and emits results safely."""
    done = pyqtSignal(list)

    def run(self):
        ff = _system_ffmpeg() or FFMPEG_BIN
        devs = list_audio_devices(ff)
        self.done.emit(devs)


class VHSCapture(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VHS Capture")
        self.setMinimumSize(900, 580)
        # Set window icon
        _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vhs_capture.ico")
        if os.path.exists(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
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

        self._log_event("info", "VHS Capture v0.2", "ready")
        if _RUST_AVAILABLE:
            self._log_event("info", "vhs_core (Rust) loaded",
                            "blue detection + BGR→RGB + FrameQueue active")
        else:
            self._log_event("stop", "vhs_core not found",
                            "run build_windows.bat then pip install the .whl")
        if not _AUDIO_AVAILABLE:
            self._log_event("stop", "sounddevice not found",
                            "pip install sounddevice  (audio meter disabled)")

        # Log FFmpeg status immediately so user knows what encoding options to expect
        sys_ff = _system_ffmpeg()
        if sys_ff:
            self._log_event("info", f"System FFmpeg found", sys_ff)
        else:
            self._log_event("stop", "System FFmpeg not found",
                            "GPU encoding (NVENC/QSV) unavailable — run: winget install ffmpeg")

        # Warn if even the bundled/PATH ffmpeg is broken (nothing will record)
        if FFMPEG_BIN != (sys_ff or "ffmpeg"):
            # We're using a bundled binary — verify it works
            try:
                r = subprocess.run([FFMPEG_BIN, "-version"],
                                   capture_output=True, timeout=5, **_NO_WINDOW)
                if r.returncode != 0:
                    self._log_event("stop", "Bundled FFmpeg broken",
                                    "recording will fail — run build_windows.bat")
            except Exception:
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

        # The actual preview area — expands to fill available space
        self.preview_label = QLabel()
        self.preview_label.setMinimumHeight(180)
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background:#000;border-radius:10px")
        self.preview_label.setText(f'<span style="color:{MUTED};font-size:14px">No camera — click Open camera</span>')
        preview_stack.addWidget(self.preview_label, 1)

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
        left.addWidget(preview_card, 1)  # stretch=1 so preview grows with window

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

        # ── RIGHT COLUMN (in a scroll area so small windows don't clip content) ───────
        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setSpacing(10)
        right.setContentsMargins(0, 0, 0, 0)

        right_scroll = QScrollArea()
        right_scroll.setWidget(right_widget)
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)
        right_scroll.setStyleSheet("QScrollArea{background:transparent;border:none}")

        # Output dir + filename prefix
        out_card, out_lay = self._section("OUTPUT")
        dir_row = QHBoxLayout()
        self.lbl_outdir = QLabel(self._output_dir)
        self.lbl_outdir.setStyleSheet(f"color:{TEXT};font-size:12px")
        self.lbl_outdir.setWordWrap(True)
        btn_dir = self._make_btn("Browse…")
        btn_dir.setFixedWidth(90)
        btn_dir.clicked.connect(self._pick_dir)
        dir_row.addWidget(self.lbl_outdir, 1)
        dir_row.addWidget(btn_dir)
        out_lay.addLayout(dir_row)

        self.edit_title = QLineEdit()
        self.edit_title.setPlaceholderText("e.g. Summer1994")
        self.edit_title.setFixedHeight(30)
        out_lay.addLayout(self._settings_row("Title prefix:", self.edit_title))

        self.edit_part = QLineEdit()
        self.edit_part.setPlaceholderText("e.g. TapeA  or  Side1")
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

        # (out_card added after tape timer — set once and forget)

        # Capture settings
        cap_card, cap_lay = self._section("CAPTURE SETTINGS")

        self.combo_res = QComboBox()
        self.combo_res.addItems(["1920×1080", "1280×720", "640×480"])
        self.combo_res.setCurrentIndex(1)
        cap_lay.addLayout(self._settings_row("Resolution:", self.combo_res))

        self.spin_fps = QSpinBox(); self.spin_fps.setRange(10, 60); self.spin_fps.setValue(25)
        cap_lay.addLayout(self._settings_row("FPS:", self.spin_fps))

        # Audio device
        self.combo_audio = QComboBox()
        self.combo_audio.addItem("Scanning…")
        self.combo_audio.setEnabled(False)
        self._audio_devices = []
        audio_row = QHBoxLayout()
        audio_lbl = QLabel("Audio device:")
        audio_lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:120px")
        btn_audio_refresh = self._make_btn("Rescan")
        btn_audio_refresh.setFixedWidth(60)
        btn_audio_refresh.setFixedHeight(30)
        btn_audio_refresh.clicked.connect(self._rescan_audio)
        audio_row.addWidget(audio_lbl)
        audio_row.addWidget(self.combo_audio, 1)
        audio_row.addWidget(btn_audio_refresh)
        cap_lay.addLayout(audio_row)
        self._audio_scanner: _AudioScanner | None = None
        self._rescan_audio()   # kick off initial scan

        self.combo_enc = QComboBox()
        self.combo_enc.addItem("Scanning encoders…")
        self.combo_enc.setEnabled(False)
        cap_lay.addLayout(self._settings_row("Encoder:", self.combo_enc))
        self._encoder_list: list[dict] = []
        self._encoders_ready = pyqtSignal(list, list)  # can't use here directly
        # Use a one-shot signal object on a helper to safely cross thread boundary
        class _EncoderSignal(QThread):
            done = pyqtSignal(list, list)
            def run(self_inner):
                working, failed = probe_encoders()
                self._encoder_list = working
                self_inner.done.emit(working, failed)
        self._enc_probe = _EncoderSignal()
        self._enc_probe.done.connect(self._populate_encoders)
        self._enc_probe.start()
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

        # Tape timer
        timer_card, timer_lay = self._section("TAPE TIMER")
        self.chk_tape_timer = QCheckBox("Auto-stop after tape length")
        self.chk_tape_timer.setChecked(False)
        self.chk_tape_timer.setStyleSheet(f"color:{MUTED};font-size:13px")
        timer_lay.addWidget(self.chk_tape_timer)

        # Common tape lengths as quick-pick buttons
        presets_row = QHBoxLayout()
        presets_row.setSpacing(6)
        TAPE_STYLE = (
            f"QPushButton{{font-size:12px;border:1px solid {BORDER};"
            f"border-radius:6px;padding:4px 10px;background:transparent;color:{MUTED}}}"
            f"QPushButton:hover{{background:{SURFACE};color:{TEXT}}}"
            f"QPushButton:checked{{background:{BLUE_BG};color:{BLUE};border-color:{BLUE}}}"
        )
        self._tape_preset_btns = []
        for label, mins in [("30m", 30), ("45m", 45), ("60m", 60), ("90m", 90), ("120m", 120)]:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setStyleSheet(TAPE_STYLE)
            b.setFixedHeight(28)
            b.clicked.connect(lambda _, m=mins: self._set_tape_minutes(m))
            presets_row.addWidget(b)
            self._tape_preset_btns.append((b, mins))
        timer_lay.addLayout(presets_row)

        # Custom duration row
        custom_row = QHBoxLayout()
        custom_lbl = QLabel("Custom (min):")
        custom_lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:100px")
        self.spin_tape_mins = QSpinBox()
        self.spin_tape_mins.setRange(1, 360)
        self.spin_tape_mins.setValue(60)
        self.spin_tape_mins.setSuffix(" min")
        INPUT_STYLE = (
            f"background:{BG};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:13px;padding:3px 8px"
        )
        self.spin_tape_mins.setStyleSheet(INPUT_STYLE)
        self.spin_tape_mins.setFixedHeight(30)
        self.spin_tape_mins.valueChanged.connect(self._sync_tape_preset_btns)
        custom_row.addWidget(custom_lbl)
        custom_row.addWidget(self.spin_tape_mins)
        custom_row.addStretch()
        timer_lay.addLayout(custom_row)

        # Remaining time display
        self.lbl_tape_remaining = QLabel("—")
        self.lbl_tape_remaining.setStyleSheet(
            f"color:{MUTED};font-size:12px;font-family:monospace")
        timer_lay.addWidget(self.lbl_tape_remaining)
        right.addWidget(timer_card)

        # Output dir + filename prefix (set-once section, lives below recording controls)
        right.addWidget(out_card)

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
        right.addStretch(0)

        root.addWidget(right_scroll, 2)

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
            best = hw_working[0]
            self._log_event("info",
                f"GPU encoding active: {best['label']}",
                f"{len(hw_working)} GPU encoder(s) available — using {best['name']}")
        else:
            self._log_event("info", "No GPU encoders available", "using CPU encoding")
        if sw_working:
            self._log_event("info",
                f"CPU encoders: {', '.join(e['name'] for e in sw_working)}", "")

        # Only log hw encoder failures if no GPU encoding is available at all,
        # or if the failure is for a different vendor than what's working.
        # (Don't spam QSV errors when NVENC is already working fine.)
        working_names = {e["name"] for e in working}
        has_nvenc = any("nvenc" in n for n in working_names)
        has_qsv   = any("qsv"   in n for n in working_names)
        for e in failed:
            enc = e["name"]
            # Suppress QSV noise when NVENC works, and vice versa
            if "qsv"   in enc and has_nvenc:
                continue
            if "nvenc" in enc and has_qsv:
                continue
            self._log_event("stop",
                f"{enc} unavailable", e.get("fail_reason", ""))

    def _populate_audio_devices(self, devices: list[str]):
        self.combo_audio.clear()
        self.combo_audio.addItem("No audio")
        for d in devices:
            self.combo_audio.addItem(d)
        self.combo_audio.setEnabled(True)
        # Auto-select if only one device or if saved pref matches
        saved = load_settings().get("audio_device", "")
        if saved:
            for i in range(self.combo_audio.count()):
                if self.combo_audio.itemText(i) == saved:
                    self.combo_audio.setCurrentIndex(i)
                    break
        elif len(devices) == 1:
            self.combo_audio.setCurrentIndex(1)
        if devices:
            self._log_event("info", f"Audio devices found: {', '.join(devices)}", "")
        else:
            self._log_event("stop", "No audio devices found",
                            "check FFmpeg dshow support")

    def _rescan_audio(self):
        self.combo_audio.clear()
        self.combo_audio.addItem("Scanning…")
        self.combo_audio.setEnabled(False)
        # Stop any previous scan
        if self._audio_scanner and self._audio_scanner.isRunning():
            self._audio_scanner.quit()
            self._audio_scanner.wait(1000)
        self._audio_scanner = _AudioScanner()
        self._audio_scanner.done.connect(self._populate_audio_devices)
        self._audio_scanner.start()

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
        self.chk_tape_timer.setChecked(cfg.get("tape_timer_enabled", False))
        self.spin_tape_mins.setValue(cfg.get("tape_timer_mins", 60))
        self._sync_tape_preset_btns(cfg.get("tape_timer_mins", 60))
        # Audio device restored in _populate_audio_devices once scan finishes

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
            "tape_timer_enabled": self.chk_tape_timer.isChecked(),
            "tape_timer_mins":    self.spin_tape_mins.value(),
            "audio_device":       "" if self.combo_audio.currentIndex() == 0
                                  else self.combo_audio.currentText(),
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
            if self._capture:
                self._audio_meter.level.connect(self._capture.set_audio_level)
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
            "audio_device":     "" if self.combo_audio.currentIndex() == 0
                                else self.combo_audio.currentText(),
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
        self.stat_clips.setText(str(len(self._clips)))
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
        self.badge_blue.setVisible(False)
        self.badge_idle.setVisible(True)
        self.badge_idle.setText("● Idle")
        self.blue_bar.setValue(0)
        self.blue_bar.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border-radius:3px;border:none}}"
            f"QProgressBar::chunk{{background:{BLUE};border-radius:3px}}"
        )
        self.lbl_tape_remaining.setText("—")
        self._log_event("stop", "Recording stopped",
                        f"{self._seg_count} blue screen splits · {len(self._clips)} clips")

    def _manual_split(self):
        if self._capture and self._recording:
            self._capture.manual_split()
            self._log_event("split", "Manual split", f"at {self.lbl_timecode.text()}")

    # ── Signals ──────────────────────────────

    def _on_frame(self, frame: np.ndarray):
        # Overlay is already drawn on the capture thread before emit.
        h, w = frame.shape[:2]
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
        # Handle audio device tracking
        if path.endswith(":audio:"):
            device = path.split(":audio:", 1)[1]
            self._log_event("info", "Audio capture started", f"device: {device}")
            return

        # Audio mux completion signal
        if path.endswith(":muxed"):
            real = path[:-6]
            self._log_event("info", "Audio muxed", Path(real).name)
            return

        # Normal segment split
        self._clips.append(path)
        self.stat_clips.setText(str(len(self._clips)))
        name = Path(path).name
        self._log_event("split", f"New segment: {name}", f"t={elapsed:.1f}s")
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
            subprocess.Popen(["explorer", "/select,", path.replace("/", "\\")],
                             **_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _set_tape_minutes(self, mins: int):
        self.spin_tape_mins.setValue(mins)
        self._sync_tape_preset_btns(mins)
        self.chk_tape_timer.setChecked(True)

    def _sync_tape_preset_btns(self, mins: int):
        for btn, bm in self._tape_preset_btns:
            btn.setChecked(bm == mins)

    def _on_timecode(self, elapsed: float):
        td = timedelta(seconds=int(elapsed))
        s  = str(td)
        self.lbl_timecode.setText(s)
        self.stat_time.setText(s)

        # Tape timer
        if self._recording and self.chk_tape_timer.isChecked():
            limit   = self.spin_tape_mins.value() * 60
            remain  = max(0, limit - int(elapsed))
            rem_str = str(timedelta(seconds=remain))
            self.lbl_tape_remaining.setText(
                f"⏱  {rem_str} remaining of {self.spin_tape_mins.value()}min"
            )
            if remain == 0:
                self._log_event("stop", "Tape timer reached",
                                f"{self.spin_tape_mins.value()}min — stopping")
                self._stop_recording()
        else:
            self.lbl_tape_remaining.setText("—")

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
        # Append one line instead of rebuilding the whole HTML each time
        colours = {
            "info": "#378add", "blue": "#5b9bd5", "resume": "#639922",
            "split": "#ba7517", "error": "#e24b4a", "rec": "#3b6d11", "stop": "#888780",
        }
        ts  = datetime.now().strftime("%H:%M:%S")
        col = colours.get(kind, "#888")
        det = f" <span style='color:#888'>{detail}</span>" if detail else ""
        html = (f"<span style='color:#888'>[{ts}]</span> "
                f"<span style='color:{col};font-weight:bold'>{msg}</span>{det}")
        self.log_view.append(html)
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
    # Set Windows taskbar app ID so the icon displays correctly
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vhs.capture.app")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    # Set application icon (window title bar, taskbar, exe)
    _icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "vhs_capture.ico")
    if os.path.exists(_icon_path):
        app.setWindowIcon(QIcon(_icon_path))

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
