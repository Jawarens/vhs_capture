"""
VHS Capture — Professional VHS Tape Digitizer & Auto-Splitter.
Records analog video to modern AV1, HEVC, H.264, or Lossless FFV1 with:
- Precision wall-clock A/V synchronization and drop-frame protection
- Native PAL (720x576) and NTSC (720x480) 4:3 standards
- Motion-adaptive deinterlacing (bwdif / yadif)
- VHS head-switching noise masking
- Multi-signal detection: Blue screen, Black screen, Static/Snow noise
- Auto-stop on tape end (blank screen + audio silence)
- Audio enhancements: Mono-to-stereo, 50/60Hz mains hum filter, loudnorm normalization
- DirectShow capture card hardware settings dialog
- Real-time disk space guard and safety auto-stop
- Instant frame snapshot (Ctrl+S)
- CMX 3600 EDL & MKV chapter metadata export

Requires: PyQt6 opencv-python numpy sounddevice imageio-ffmpeg
FFmpeg is bundled via imageio-ffmpeg if not found on system PATH.
"""

__version__ = "0.4.0"

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
import platform

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QGroupBox, QTextEdit, QComboBox,
    QSpinBox, QDoubleSpinBox, QFileDialog, QFrame, QProgressBar, QCheckBox,
    QGridLayout, QDialog, QDialogButtonBox, QListWidget,
    QListWidgetItem, QLineEdit, QScrollArea, QSizePolicy, QInputDialog
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QMutex, QMutexLocker,
    PYQT_VERSION_STR, qVersion
)
from PyQt6.QtGui import (
    QImage, QPixmap, QColor, QPalette, QTextCursor, QPainter, QPen, QIcon,
    QShortcut, QKeySequence
)

# In windowed PyInstaller on Windows, sys.stdout and sys.stderr may be None.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

def resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)

try:
    import sounddevice as sd
    _AUDIO_AVAILABLE = True
except Exception:
    _AUDIO_AVAILABLE = False

# ── Resolve FFmpeg binary ──────────────────────────────────────────────────
# Priority: hardware-capable system FFmpeg → PATH → imageio-ffmpeg bundled binary → error
def _system_ffmpeg() -> str | None:
    """
    Find a system FFmpeg that supports hw encoders (NVENC/QSV).
    Checks PATH first, then common Windows install locations.
    """
    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", "C:\\Program Files")
        prog_files_x86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
        user_profile = os.environ.get("USERPROFILE", "")
        prog_data = os.environ.get("ProgramData", "C:\\ProgramData")

        candidates = [
            os.path.join(prog_data, "chocolatey", "bin", "ffmpeg.exe"),
            os.path.join(local_app, "Microsoft", "WinGet", "Packages",
                         "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
                         "ffmpeg-7.1-full_build", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ProgramData", "chocolatey", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "tools", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current",
                         "bin", "ffmpeg.exe"),
            os.path.join(prog_data, "scoop", "apps", "ffmpeg",
                         "current", "bin", "ffmpeg.exe"),
            os.path.join(prog_files, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(prog_files_x86, "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "ffmpeg", "ffmpeg.exe"),
            os.path.join(os.path.dirname(sys.executable), "ffmpeg.exe"),
        ]

        winget_pkg_dir = os.path.join(local_app, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(winget_pkg_dir):
            for entry in os.listdir(winget_pkg_dir):
                if "ffmpeg" in entry.lower() or "gyan" in entry.lower():
                    for root, _, files in os.walk(os.path.join(winget_pkg_dir, entry)):
                        for f in files:
                            if f.lower() == "ffmpeg.exe":
                                candidates.append(os.path.join(root, f))

        for c in candidates:
            if os.path.isfile(c):
                return c

    return None

def _find_ffmpeg() -> str:
    """Return the path to a working ffmpeg binary, prioritizing hardware-capable system FFmpeg."""
    sys_ff = _system_ffmpeg()
    if sys_ff:
        return sys_ff
    # Fallback to imageio-ffmpeg bundled binary (works inside PyInstaller .exe too)
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
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
    _RUST_AVAILABLE = hasattr(_vhs_core, "detect_blue") and hasattr(_vhs_core, "FrameQueue")
    if not _RUST_AVAILABLE:
        _vhs_core = None
except (ImportError, Exception):
    _vhs_core = None
    _RUST_AVAILABLE = False


def _numpy_blue_ratio(small: np.ndarray, sample_w: int, sample_h: int) -> float:
    """NumPy implementation of VHS blue detection."""
    b_ch  = small[:, :, 0].astype(np.float32)
    g_ch  = small[:, :, 1].astype(np.float32)
    r_ch  = small[:, :, 2].astype(np.float32)
    maxc  = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    minc  = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    d     = maxc - minc + 1e-6
    sat   = np.divide(d, maxc, out=np.zeros_like(maxc), where=maxc > 0)
    hue   = np.zeros_like(maxc)
    mask_r = maxc == r_ch
    mask_g = (maxc == g_ch) & ~mask_r
    mask_b = ~mask_r & ~mask_g
    hue[mask_r] = (60 * ((g_ch[mask_r] - b_ch[mask_r]) / d[mask_r]) + 360) % 360
    hue[mask_g] =  60 * ((b_ch[mask_g] - r_ch[mask_g]) / d[mask_g]) + 120
    hue[mask_b] =  60 * ((r_ch[mask_b] - g_ch[mask_b]) / d[mask_b]) + 240
    blue_mask = (hue >= 190) & (hue <= 265) & (sat >= 0.20) & (b_ch > 60)
    return float(blue_mask.sum()) / (sample_w * sample_h)


def analyse_signals(frame: np.ndarray, sample_w: int = 80, sample_h: int = 45) -> dict:
    """
    Downsamples frame and detects:
      - blue_ratio: fraction of pixels matching VHS blue
      - black_ratio: fraction of pixels with luma < 22 (blank tape)
      - avg_luma: mean brightness (0-255)
      - is_static: analog static/snow RF noise
    """
    small = cv2.resize(frame, (sample_w, sample_h), interpolation=cv2.INTER_NEAREST)

    # 1. Blue ratio (Rust SIMD hot-path or NumPy fallback)
    if _RUST_AVAILABLE:
        try:
            blue_ratio = _vhs_core.detect_blue(
                small.tobytes(), sample_w, sample_h, 0.0, sample_w, sample_h
            )
        except Exception:
            blue_ratio = _numpy_blue_ratio(small, sample_w, sample_h)
    else:
        blue_ratio = _numpy_blue_ratio(small, sample_w, sample_h)

    # 2. Black ratio & Luma
    b_ch = small[:, :, 0].astype(np.float32)
    g_ch = small[:, :, 1].astype(np.float32)
    r_ch = small[:, :, 2].astype(np.float32)
    luma = b_ch * 0.114 + g_ch * 0.587 + r_ch * 0.299
    avg_luma = float(np.mean(luma))
    black_ratio = float(np.sum(luma < 22.0)) / (sample_w * sample_h)

    # 3. Static / Snow Noise
    # Analog static has high spatial standard deviation in luma, mid-gray mean, and low saturation
    luma_std = float(np.std(luma))
    maxc = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    minc = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    d = maxc - minc + 1e-6
    sat = np.divide(d, maxc, out=np.zeros_like(maxc), where=maxc > 0)
    sat_mean = float(np.mean(sat))
    is_static = bool((luma_std > 45.0) and (35.0 <= avg_luma <= 170.0) and (sat_mean < 0.25) and (blue_ratio < 0.2))

    return {
        "blue_ratio": blue_ratio,
        "black_ratio": black_ratio,
        "avg_luma": avg_luma,
        "is_static": is_static,
    }


def analyse_blue_ratio(frame: np.ndarray, sample_w: int = 80, sample_h: int = 45) -> float:
    """Backwards-compatible wrapper returning only blue ratio."""
    return analyse_signals(frame, sample_w, sample_h)["blue_ratio"]


def probe_clip_is_empty(clip_path: str, min_duration: float = 2.0, min_bytes: int = 150000) -> tuple[bool, str]:
    """
    Inspects a recorded clip to determine if it has nothing in it:
      - File missing or size < min_bytes (unusable/header only)
      - Zero video/audio streams or duration < min_duration
      - All sampled frames are blue screen, black screen, or analog static
    Returns (is_empty: bool, reason: str).
    """
    p = Path(clip_path)
    if not p.exists():
        return True, "File does not exist"

    try:
        sz = p.stat().st_size
    except Exception as e:
        return True, f"Unreadable file: {e}"

    if sz < min_bytes:
        return True, f"File too small ({sz} bytes, threshold {min_bytes} bytes)"

    # Probe duration and frame count via OpenCV
    cap = cv2.VideoCapture(str(p))
    if not cap.isOpened():
        return True, "Unable to open video stream"

    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        dur = (frame_count / fps) if (frame_count > 0 and fps > 0) else 0.0

        if frame_count <= 0 or dur < min_duration:
            return True, f"Duration too short ({dur:.2f}s, threshold {min_duration}s, {frame_count} frames)"

        # Sample up to 10 frames across the clip to test if there's any active content
        sample_indices = [
            int(idx) for idx in np.linspace(0, max(0, frame_count - 1), num=min(10, frame_count), dtype=int)
        ]
        non_empty_found = False

        for f_idx in sample_indices:
            cap.set(cv2.CAP_PROP_POS_FRAMES, f_idx)
            ret, frame = cap.read()
            if not ret or frame is None:
                continue

            sig = analyse_signals(frame)
            # If a frame has < 80% blue, < 80% black, and is not pure static noise, it contains real content
            if sig["blue_ratio"] < 0.80 and sig["black_ratio"] < 0.80 and not sig["is_static"]:
                non_empty_found = True
                break

        if not non_empty_found:
            return True, "All sampled frames were blank (blue screen, black screen, or static)"

        return False, ""
    except Exception as e:
        return False, f"Probe error: {e}"
    finally:
        cap.release()


# ─────────────────────────────────────────────
#  Settings persistence
# ─────────────────────────────────────────────

_SETTINGS_PATH = Path.home() / ".vhs_capture_settings.json"
_PROFILES_DIR  = Path.home() / ".vhs_capture_profiles"

_FACTORY_PRESETS: dict[str, dict] = {
    "PAL — Hauppauge USB-Live2 (Optimal)": {
        "resolution": "PAL (720x576)",
        "fps": 25.0,
        "aspect_ratio": "4:3",
        "deinterlace": "bwdif",
        "mask_head_switching": True,
        "audio_hum_filter": True,
        "audio_normalize": True,
        "audio_channel_mode": "stereo",
        "auto_split_blue": True,
        "auto_split_black": False,
        "auto_stop_tape_end": True,
        "tape_end_timeout": 30,
        "crf": 23,
    },
    "PAL — Universum VHS (bwdif 50fps)": {
        "resolution": "PAL (720x576)",
        "fps": 25.0,
        "aspect_ratio": "4:3",
        "deinterlace": "bwdif",
        "mask_head_switching": True,
        "audio_hum_filter": True,
        "audio_normalize": False,
        "audio_channel_mode": "stereo",
        "auto_split_blue": True,
        "auto_split_black": False,
        "auto_stop_tape_end": True,
        "tape_end_timeout": 30,
        "crf": 26,
    },
    "Archival Master (Lossless FFV1 + PCM)": {
        "resolution": "PAL (720x576)",
        "fps": 25.0,
        "aspect_ratio": "4:3",
        "deinterlace": "bwdif",
        "mask_head_switching": False,
        "audio_hum_filter": False,
        "audio_normalize": False,
        "audio_channel_mode": "stereo",
        "auto_split_blue": False,
        "auto_split_black": False,
        "auto_stop_tape_end": True,
        "tape_end_timeout": 45,
        "crf": 0,
        "encoder": "ffv1",
    },
    "NTSC — 60fps Fluid": {
        "resolution": "NTSC (720x480)",
        "fps": 29.97,
        "aspect_ratio": "4:3",
        "deinterlace": "bwdif",
        "mask_head_switching": True,
        "audio_hum_filter": True,
        "audio_normalize": True,
        "audio_channel_mode": "stereo",
        "auto_split_blue": True,
        "auto_split_black": False,
        "auto_stop_tape_end": True,
        "tape_end_timeout": 30,
        "crf": 24,
    },
    "Compact Web Share (AV1 / H.264 Fast)": {
        "resolution": "PAL (720x576)",
        "fps": 25.0,
        "aspect_ratio": "4:3",
        "deinterlace": "bwdif",
        "mask_head_switching": True,
        "audio_hum_filter": True,
        "audio_normalize": True,
        "audio_channel_mode": "stereo",
        "auto_split_blue": True,
        "auto_split_black": False,
        "auto_stop_tape_end": True,
        "tape_end_timeout": 30,
        "crf": 30,
    },
}

def load_settings() -> dict:
    try:
        return json.loads(_SETTINGS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_settings(d: dict):
    try:
        _SETTINGS_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass

def list_profiles() -> list[str]:
    """Return all available preset names followed by saved user profile names."""
    names = list(_FACTORY_PRESETS.keys())
    if _PROFILES_DIR.is_dir():
        for p in sorted(_PROFILES_DIR.glob("*.json")):
            stem = p.stem
            if stem not in names:
                names.append(stem)
    return names

def load_profile(name: str) -> dict:
    if name in _FACTORY_PRESETS:
        return dict(_FACTORY_PRESETS[name])
    p = _PROFILES_DIR / f"{name}.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}

def save_profile(name: str, data: dict):
    _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    p = _PROFILES_DIR / f"{name}.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")

def delete_profile(name: str) -> bool:
    p = _PROFILES_DIR / f"{name}.json"
    if p.is_file():
        try:
            p.unlink()
            return True
        except Exception:
            pass
    return False


# ─────────────────────────────────────────────
#  Audio meter thread
# ─────────────────────────────────────────────

class AudioMeterThread(QThread):
    """Reads microphone/line-in and emits RMS level 20x/sec."""
    level = pyqtSignal(float)   # 0.0–1.0

    def __init__(self, device_name: str = ""):
        super().__init__()
        self.device_name = device_name
        self._running = False

    def run(self):
        if not _AUDIO_AVAILABLE:
            return
        self._running = True
        try:
            device_idx = None
            if self.device_name and self.device_name.lower() != "no audio":
                try:
                    for i, d in enumerate(sd.query_devices()):
                        if d.get("max_input_channels", 0) > 0 and self.device_name.lower() in d["name"].lower():
                            device_idx = i
                            break
                except Exception:
                    pass

            with sd.InputStream(device=device_idx, channels=1, dtype='float32') as stream:
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


# Full encoder profiles including Lossless Archival Master (FFV1)
_ENCODER_PROFILES = [
    # ── NVIDIA NVENC ─────────────────────────────────────────────────
    {
        "name": "av1_nvenc", "label": "AV1  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    {
        "name": "hevc_nvenc", "label": "HEVC  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    {
        "name": "h264_nvenc", "label": "H.264  —  NVIDIA NVENC ⚡",
        "is_hw": True, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True, "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    # ── Intel QuickSync ──────────────────────────────────────────────
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
    # ── Software / CPU ───────────────────────────────────────────────
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
    # ── Archival Lossless ────────────────────────────────────────────
    {
        "name": "ffv1", "label": "FFV1  —  Lossless Archival Master (MKV)",
        "is_hw": False, "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False, "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1", "-threads", "0",
        ],
    },
]


def _friendly_hw_error(enc: str, stderr: str) -> str:
    s = stderr.lower()
    is_qsv   = "qsv" in enc
    is_nvenc = "nvenc" in enc

    if is_qsv:
        if "invalid argument" in s or "error code: -22" in s:
            return (f"{enc}: Intel MFX/OneVPL runtime not installed. "
                    "Install Intel Graphics Driver and oneVPL GPU Runtime")
        if "mfx session" in s or "mfxinit" in s or "error creating" in s:
            return f"{enc}: MFX session failed — install Intel oneVPL GPU Runtime"
        if "unsupported" in s or "not support" in s:
            return f"{enc}: Intel GPU too old for this codec (requires 6th gen+)"
        if "no capable devices" in s or "device open failed" in s:
            return f"{enc}: no Intel GPU found or driver not loaded"

    if is_nvenc:
        if "invalid argument" in s or "error code: -22" in s:
            return f"{enc}: invalid argument — driver mismatch or codec not supported (AV1 NVENC requires RTX 4000+)"
        if "libcuda" in s or "cannot load nvcuda" in s or "no cuda" in s:
            return f"{enc}: NVIDIA driver not found — install Game Ready or Studio driver"
        if "no capable devices" in s or "device open failed" in s:
            return f"{enc}: no capable NVIDIA GPU found"
        if "not enough" in s and "surface" in s:
            return f"{enc}: not enough VRAM surfaces — try a lower resolution"

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
#  FFmpeg pipe writer
# ─────────────────────────────────────────────

class FFmpegWriter:
    """Wraps an ffmpeg process that accepts raw BGR frames on stdin."""

    def __init__(self, output_path: str, width: int, height: int, fps: float,
                 encoder: str = "libsvtav1", crf: int = 28,
                 encoder_info: dict | None = None,
                 aspect_ratio: str = "4:3",
                 deinterlace: str = "off",
                 mask_head_switching: bool = True):
        self.output_path  = output_path
        self.width        = width
        self.height       = height
        self.fps          = fps
        self.encoder      = encoder
        self.crf          = crf
        self.encoder_info = encoder_info
        self.aspect_ratio = aspect_ratio
        self.deinterlace  = deinterlace
        self.mask_head_switching = mask_head_switching
        self._proc        = None
        self._lock        = threading.Lock()
        self._stderr_lines: list[str] = []

    def start(self):
        info    = self.encoder_info or {}
        enc     = self.encoder
        pixfmt  = info.get("pixfmt", "yuv420p")
        ff      = info.get("ffmpeg_bin", FFMPEG_BIN)

        args_fn  = info.get("args")
        enc_args = args_fn(self.crf, self.width, self.height) if args_fn \
                   else ["-crf", str(self.crf), "-threads", "0"]

        # Build video filter chain (deinterlace + head switching mask)
        vf_filters = []
        if self.mask_head_switching:
            # Replaces the bottom 12 noisy scanlines with a clean solid black bar
            vf_filters.append("drawbox=y=ih-12:h=12:color=black:t=fill")

        is_nvenc = "nvenc" in enc
        has_cuda_filters = False
        if is_nvenc and self.deinterlace in ("bwdif", "bwdif_2x", "yadif"):
            # Accelerate deinterlacing directly on CUDA cores (offloads CPU to GPU)
            vf_filters.append("format=nv12")
            vf_filters.append("hwupload_cuda")
            if self.deinterlace == "bwdif":
                vf_filters.append("bwdif_cuda=mode=0:parity=-1:deint=1")
            elif self.deinterlace == "bwdif_2x":
                vf_filters.append("bwdif_cuda=mode=1:parity=-1:deint=1")
            elif self.deinterlace == "yadif":
                vf_filters.append("yadif_cuda=mode=0:parity=-1:deint=1")
            has_cuda_filters = True
        else:
            if self.deinterlace == "bwdif":
                vf_filters.append("bwdif=mode=0:parity=-1:deint=1")
            elif self.deinterlace == "bwdif_2x":
                vf_filters.append("bwdif=mode=1:parity=-1:deint=1")
            elif self.deinterlace == "yadif":
                vf_filters.append("yadif=mode=0:parity=-1:deint=1")

        vf_args = ["-vf", ",".join(vf_filters)] if vf_filters else []
        pixfmt_args = [] if has_cuda_filters else ["-pix_fmt", pixfmt]

        cmd = [
            ff, "-y",
            "-thread_queue_size", "512",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", f"{self.width}x{self.height}",
            "-r", str(self.fps),
            "-i", "pipe:0",
        ] + vf_args + [
            "-c:v", enc,
        ] + enc_args + pixfmt_args + [
            "-aspect", self.aspect_ratio,
        ]

        if self.output_path.lower().endswith(".mp4"):
            cmd += ["-movflags", "+faststart"]

        cmd.append(self.output_path)

        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **_NO_WINDOW
        )
        self._stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self):
        if self._proc and self._proc.stderr:
            for line in self._proc.stderr:
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                if decoded.startswith("frame=") or decoded.startswith("size="):
                    continue
                if decoded.startswith("video:") or decoded.startswith("audio:"):
                    continue
                self._stderr_lines.append(decoded)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines = self._stderr_lines[-100:]

    def last_errors(self, n=5) -> list[str]:
        return self._stderr_lines[-n:]

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
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                self._proc = None

    @property
    def running(self):
        return self._proc is not None


# ─────────────────────────────────────────────
#  Audio device enumeration
# ─────────────────────────────────────────────

def list_audio_devices(ffmpeg_bin: str) -> list[str]:
    """Enumerate audio input devices available to FFmpeg."""
    devices = []

    if sys.platform == "win32":
        cmd = [ffmpeg_bin, "-list_devices", "true",
               "-f", "dshow", "-i", "dummy",
               "-loglevel", "debug"]
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=6, **_NO_WINDOW)
            output = r.stderr.decode(errors="replace")
            import re
            for line in output.splitlines():
                if "(audio)" in line.lower():
                    m = re.search(r'"([^"]+)"', line)
                    if m:
                        name = m.group(1)
                        if name not in devices and name != "dummy":
                            devices.append(name)
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
        try:
            r = subprocess.run(["pactl", "list", "sources", "short"],
                               capture_output=True, timeout=5, **_NO_WINDOW)
            for line in r.stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 2:
                    devices.append(parts[1])
        except Exception:
            pass

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
    Captures audio into raw PCM and muxes with precision A/V async resample,
    mono duplication, 50/60Hz mains hum suppression, and normalization.
    """

    def __init__(self, device_name: str, temp_path: str, ffmpeg_bin: str,
                 channel_mode: str = "stereo",
                 hum_filter: bool = False,
                 normalize: bool = False,
                 is_lossless: bool = False):
        self.device_name = device_name
        self.temp_path   = temp_path
        self.ffmpeg_bin  = ffmpeg_bin
        self.channel_mode = channel_mode
        self.hum_filter = hum_filter
        self.normalize = normalize
        self.is_lossless = is_lossless
        self._proc: subprocess.Popen | None = None

    def start(self):
        if sys.platform == "win32":
            device_arg = f"audio={self.device_name}"
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "dshow",
                "-analyzeduration", "4000000",
                "-probesize", "4000000",
                "-i", device_arg,
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "-thread_queue_size", "2048",
                self.temp_path,
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

        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **_NO_WINDOW
        )
        def _drain_stderr():
            if self._proc and self._proc.stderr:
                for line in self._proc.stderr:
                    pass
        threading.Thread(target=_drain_stderr, daemon=True, name="audio-stderr").start()

    def stop_and_mux(self, video_path: str, video_writer: FFmpegWriter | None = None,
                     on_done=None, on_error=None):
        def _work():
            if video_writer:
                video_writer.stop()

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

            time.sleep(0.3)

            pcm = Path(self.temp_path)
            vpath = Path(video_path)

            if not vpath.exists():
                pcm.unlink(missing_ok=True)
                if on_error:
                    on_error(f"Video file missing: {video_path}")
                return

            if not pcm.exists() or pcm.stat().st_size < 20480:
                pcm.unlink(missing_ok=True)
                if on_error:
                    on_error("Audio file empty or missing — saved video only")
                if on_done:
                    on_done()
                return

            # Construct audio filter graph:
            # 1. aresample=async=1000 ensures smooth hardware clock alignment and zero A/V drift
            af_filters = ["aresample=async=1000"]
            if self.channel_mode == "mono_left":
                af_filters.append("pan=stereo|c0=c0|c1=c0")
            elif self.channel_mode == "mono_right":
                af_filters.append("pan=stereo|c0=c1|c1=c1")

            if self.hum_filter:
                # 65Hz highpass filter cleans 50Hz/60Hz mains hum from VCR ground loops
                af_filters.append("highpass=f=65")

            if self.normalize:
                # EBU R128 loudness normalization
                af_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

            af_arg = ",".join(af_filters)

            out_tmp = vpath.with_suffix(".mux_tmp" + vpath.suffix)
            is_mp4  = vpath.suffix.lower() == ".mp4"
            is_mkv  = vpath.suffix.lower() == ".mkv"

            # Use lossless FLAC for MKV/FFV1 or AAC 192k for MP4/delivery
            audio_codec = ["-c:a", "flac"] if (is_mkv and self.is_lossless) else ["-c:a", "aac", "-b:a", "192k"]

            cmd = [
                self.ffmpeg_bin, "-y",
                "-i", str(vpath),
                "-f", "s16le", "-ar", "48000", "-ac", "2",
                "-i", str(pcm),
                "-c:v", "copy",
                "-af", af_arg,
            ] + audio_codec + [
                "-shortest",
            ]
            if is_mp4:
                cmd += ["-movflags", "+faststart"]
            cmd.append(str(out_tmp))

            try:
                r = subprocess.run(cmd, capture_output=True, timeout=180, **_NO_WINDOW)
                if r.returncode == 0 and out_tmp.exists():
                    replaced = False
                    for _ in range(5):
                        try:
                            out_tmp.replace(vpath)
                            replaced = True
                            break
                        except OSError:
                            time.sleep(0.2)
                    if not replaced:
                        out_tmp.replace(vpath)
                    if on_done:
                        on_done()
                else:
                    err = r.stderr.decode(errors="replace")[-400:]
                    if on_error:
                        on_error(f"Mux failed (rc={r.returncode}): {err}")
                    out_tmp.unlink(missing_ok=True)
                    if on_done:
                        on_done()
            except Exception as e:
                if on_error:
                    on_error(str(e))
                out_tmp.unlink(missing_ok=True)
                if on_done:
                    on_done()
            finally:
                pcm.unlink(missing_ok=True)

        threading.Thread(target=_work, daemon=True, name="audio-mux").start()


# ─────────────────────────────────────────────
#  Writer Thread
# ─────────────────────────────────────────────

class _WriterThread(threading.Thread):
    """Dedicated thread that drains frame queue into active FFmpegWriter."""
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
        self._has_item = threading.Event()
        self._on_error = on_error

    def set_writer(self, w: FFmpegWriter | None) -> FFmpegWriter | None:
        with self._lock:
            old = self._writer
            if old:
                self._flush_queue_to(old)
            self._writer = w
        return old

    def _flush_queue_to(self, target_writer: FFmpegWriter):
        if self._use_rust:
            while True:
                raw = self._q.pop()
                if raw is None:
                    break
                target_writer.write_raw(raw)
        else:
            while not self._q.empty():
                try:
                    raw = self._q.get_nowait()
                    target_writer.write_raw(raw)
                except _queue.Empty:
                    break

    def push(self, frame: np.ndarray):
        raw = frame.tobytes()
        if self._use_rust:
            self._q.push(raw)
        else:
            try:
                self._q.put_nowait(raw)
            except _queue.Full:
                pass
        self._has_item.set()

    def run(self):
        while self._running:
            if self._use_rust:
                raw = self._q.pop()
                if raw is None:
                    self._has_item.clear()
                    self._has_item.wait(timeout=0.05)
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
        self._has_item.set()
        self.join(timeout=5)
        with self._lock:
            if self._writer:
                self._writer.stop()
                self._writer = None


# ─────────────────────────────────────────────
#  Capture Thread
# ─────────────────────────────────────────────

_PREVIEW_FPS = 25
_DETECT_EVERY_N = 3

class CaptureThread(QThread):
    frame_ready    = pyqtSignal(object)           # preview frame (BGR ndarray)
    blue_changed   = pyqtSignal(bool, float)      # is_blue, ratio
    black_changed  = pyqtSignal(bool, float)     # is_black, ratio
    static_changed = pyqtSignal(bool)             # is_static
    segment_split  = pyqtSignal(str, float)       # output path, timestamp
    timecode_tick  = pyqtSignal(float)            # seconds elapsed
    sync_stats     = pyqtSignal(int, int)         # frames_written, frames_duplicated
    tape_ended     = pyqtSignal(str)              # reason message
    error          = pyqtSignal(str)

    def __init__(self, config: dict):
        super().__init__()
        self.cfg = config
        self._running = False
        self._recording = False
        self._segment_index = 0
        self._start_time = 0.0
        self._in_blue = False
        self._in_black = False
        self._last_audio_val = 0.0
        self._mutex = QMutex()
        self._cap: cv2.VideoCapture | None = None
        self._writer_thread = _WriterThread(
            on_error=lambda errs: self.error.emit("FFmpeg: " + errs[-1])
        )
        self._writer_thread.start()
        self._audio_recorder: AudioRecorder | None = None
        self._current_video_path: str = ""

        # A/V sync and drop-frame accounting
        self._frames_written = 0
        self._frames_duplicated = 0
        self._last_written_frame: np.ndarray | None = None

    def open_device_settings(self) -> bool:
        """Pops up native DirectShow hardware properties (composite/s-video, PAL/NTSC, proc amp)."""
        if self._cap and self._cap.isOpened():
            try:
                # cv2.CAP_PROP_SETTINGS (38) is DirectShow property sheet
                return bool(self._cap.set(cv2.CAP_PROP_SETTINGS, 1))
            except Exception:
                return False
        return False

    def start_recording(self):
        with QMutexLocker(self._mutex):
            self._segment_index = 0
            self._start_time = time.time()
            self._frames_written = 0
            self._frames_duplicated = 0
            self._last_written_frame = None
            self._recording = True
            self._start_new_segment()

    def stop_recording(self):
        with QMutexLocker(self._mutex):
            self._recording = False
        old_w = self._writer_thread.set_writer(None)
        prev_video = self._current_video_path
        self._current_video_path = ""
        self._finish_audio_segment(prev_video, old_w)

    def manual_split(self):
        with QMutexLocker(self._mutex):
            if not self._recording:
                return
        self._start_new_segment()

    def set_audio_level(self, val: float):
        self._last_audio_val = val

    def _finish_audio_segment(self, video_path: str, video_writer: FFmpegWriter | None = None):
        ar = self._audio_recorder
        self._audio_recorder = None
        if ar and video_path:
            ar.stop_and_mux(
                video_path,
                video_writer=video_writer,
                on_done=lambda: self.segment_split.emit(video_path + ":muxed", 0.0),
                on_error=lambda e: self.error.emit(f"Audio mux: {e}"),
            )
        elif video_path:
            def _stop_video_only():
                if video_writer:
                    video_writer.stop()
                self.segment_split.emit(video_path + ":muxed", 0.0)
            threading.Thread(target=_stop_video_only, daemon=True).start()

    def _start_new_segment(self):
        cfg = self.cfg
        out_dir = Path(cfg["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")

        is_lossless = (cfg.get("encoder") == "ffv1")
        ext = "mkv" if is_lossless else "mp4"

        title  = cfg.get("title_prefix", "").strip()
        part   = cfg.get("part_prefix", "").strip()
        clip_n = f"clip{self._segment_index+1:02d}"
        parts  = [p for p in [title, part, clip_n, ts] if p]
        fname  = out_dir / f"{'_'.join(parts)}.{ext}"
        self._segment_index += 1

        # Start new video writer with aspect ratio and deinterlacing
        new_w = FFmpegWriter(
            str(fname),
            cfg["width"], cfg["height"], cfg["fps"],
            encoder=cfg["encoder"], crf=cfg["crf"],
            encoder_info=cfg.get("encoder_info"),
            aspect_ratio=cfg.get("aspect_ratio", "4:3"),
            deinterlace=cfg.get("deinterlace", "off"),
            mask_head_switching=cfg.get("mask_head_switching", True),
        )
        new_w.start()

        old_w = self._writer_thread.set_writer(new_w)

        prev_video = self._current_video_path
        self._current_video_path = str(fname)
        self._finish_audio_segment(prev_video, old_w)

        # Start audio recorder with filtering
        audio_dev = cfg.get("audio_device", "")
        sys_ff = _system_ffmpeg() or FFMPEG_BIN
        if audio_dev:
            tmp_pcm = str(out_dir / f".audio_tmp_{self._segment_index:03d}.pcm")
            ar = AudioRecorder(
                audio_dev, tmp_pcm, sys_ff,
                channel_mode=cfg.get("audio_channel_mode", "stereo"),
                hum_filter=cfg.get("audio_hum_filter", False),
                normalize=cfg.get("audio_normalize", False),
                is_lossless=is_lossless
            )
            ar.start()
            self._audio_recorder = ar

        elapsed = time.time() - self._start_time if self._start_time else 0.0
        self.segment_split.emit(str(fname), elapsed)

    def run(self):
        cfg = self.cfg

        # Use CAP_DSHOW on Windows to enable device properties dialog and clean SD capturing
        backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
        cap = cv2.VideoCapture(cfg["camera_index"], backend)
        if not cap.isOpened() and backend != cv2.CAP_ANY:
            cap = cv2.VideoCapture(cfg["camera_index"])

        cap.set(cv2.CAP_PROP_FRAME_WIDTH,  cfg["width"])
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, cfg["height"])
        cap.set(cv2.CAP_PROP_FPS,          cfg["fps"])
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        if not cap.isOpened():
            self.error.emit("Could not open camera. Check the camera index.")
            return

        self._cap = cap
        self._running = True
        preview_interval  = 1.0 / _PREVIEW_FPS
        timecode_interval = 0.5

        last_preview  = 0.0
        last_timecode = 0.0
        frame_count   = 0

        def _snapshot_cfg():
            return {
                "blue_threshold":     cfg.get("blue_threshold", 0.60),
                "min_blue_secs":      cfg.get("min_blue_seconds", 1.5),
                "auto_split_blue":    cfg.get("auto_split_blue", True),
                "black_threshold":    cfg.get("black_threshold", 0.85),
                "min_black_secs":     cfg.get("min_black_seconds", 3.0),
                "auto_split_black":   cfg.get("auto_split_black", False),
                "auto_stop_tape_end": cfg.get("auto_stop_tape_end", True),
                "tape_end_timeout":   cfg.get("tape_end_timeout", 30.0),
                "silence_threshold":  cfg.get("silence_threshold", 0.04),
                "fps":                float(cfg.get("fps", 25)),
            }
        loop = _snapshot_cfg()

        blue_start_time: float | None = None
        blue_resume_time: float | None = None
        black_start_time: float | None = None
        black_resume_time: float | None = None
        blank_start_time: float | None = None
        silence_start_time: float | None = None
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
                fh, fw = frame.shape[:2]
                pw = 640
                ph = max(180, int(round(pw * fh / fw)))
                preview = cv2.resize(frame, (pw, ph), interpolation=cv2.INTER_LINEAR)

                if self._recording:
                    audio_val = self._last_audio_val
                    bar_h = max(2, int(audio_val * 40))
                    bar_w = 8
                    x = pw - bar_w - 10
                    y_bot = ph - 10
                    cv2.rectangle(preview, (x, y_bot - 40), (x + bar_w, y_bot), (40, 40, 40), -1)
                    colour = (0, 200, 40) if audio_val < 0.7 else (0, 200, 200) if audio_val < 0.9 else (0, 40, 220)
                    cv2.rectangle(preview, (x, y_bot - bar_h), (x + bar_w, y_bot), colour, -1)
                    cv2.circle(preview, (14, 14), 6, (0, 0, 200), -1)

                self.frame_ready.emit(preview)

            # ── Signal analysis (Blue, Black, Static, Tape-End) ──
            if frame_count % _DETECT_EVERY_N == 0:
                sig = analyse_signals(frame)
                ratio_blue  = sig["blue_ratio"]
                ratio_black = sig["black_ratio"]
                is_static   = sig["is_static"]

                is_blue_now  = ratio_blue >= loop["blue_threshold"]
                is_black_now = ratio_black >= loop["black_threshold"]

                # 1. Blue screen detection
                if is_blue_now:
                    blue_resume_time = None
                    if blue_start_time is None:
                        blue_start_time = now
                    if now - blue_start_time >= loop["min_blue_secs"] and not self._in_blue:
                        self._in_blue = True
                        self.blue_changed.emit(True, ratio_blue)
                        if loop["auto_split_blue"]:
                            with QMutexLocker(self._mutex):
                                if self._recording:
                                    old_w = self._writer_thread.set_writer(None)
                                    old_vid = self._current_video_path
                                    self._current_video_path = ""
                                    self._finish_audio_segment(old_vid, old_w)
                else:
                    blue_start_time = None
                    if self._in_blue:
                        if blue_resume_time is None:
                            blue_resume_time = now
                        elif now - blue_resume_time >= HYSTERESIS_SECS:
                            self._in_blue = False
                            blue_resume_time = None
                            self.blue_changed.emit(False, ratio_blue)
                            if loop["auto_split_blue"]:
                                with QMutexLocker(self._mutex):
                                    if self._recording:
                                        self._start_new_segment()
                                loop = _snapshot_cfg()

                # 2. Black screen detection
                if is_black_now:
                    black_resume_time = None
                    if black_start_time is None:
                        black_start_time = now
                    if now - black_start_time >= loop["min_black_secs"] and not self._in_black:
                        self._in_black = True
                        self.black_changed.emit(True, ratio_black)
                        if loop["auto_split_black"]:
                            with QMutexLocker(self._mutex):
                                if self._recording:
                                    old_w = self._writer_thread.set_writer(None)
                                    old_vid = self._current_video_path
                                    self._current_video_path = ""
                                    self._finish_audio_segment(old_vid, old_w)
                else:
                    black_start_time = None
                    if self._in_black:
                        if black_resume_time is None:
                            black_resume_time = now
                        elif now - black_resume_time >= HYSTERESIS_SECS:
                            self._in_black = False
                            black_resume_time = None
                            self.black_changed.emit(False, ratio_black)
                            if loop["auto_split_black"]:
                                with QMutexLocker(self._mutex):
                                    if self._recording:
                                        self._start_new_segment()
                                loop = _snapshot_cfg()

                self.static_changed.emit(is_static)

                # 3. Unattended End-of-Tape Detection (Blank/Blue/Static + Silence)
                is_blank_now = is_blue_now or is_black_now or is_static
                if is_blank_now:
                    if blank_start_time is None:
                        blank_start_time = now
                else:
                    blank_start_time = None

                if self._last_audio_val < loop["silence_threshold"]:
                    if silence_start_time is None:
                        silence_start_time = now
                else:
                    silence_start_time = None

                if loop["auto_stop_tape_end"] and self._recording:
                    if blank_start_time and silence_start_time:
                        dead_air = now - max(blank_start_time, silence_start_time)
                        if dead_air >= loop["tape_end_timeout"]:
                            cond = "Blue screen" if is_blue_now else "Black screen" if is_black_now else "Static noise"
                            self.tape_ended.emit(f"{cond} and audio silence for {dead_air:.0f}s")
                            blank_start_time = None
                            silence_start_time = None

            # ── Write frame with A/V sync & drop-frame protection ────────────
            with QMutexLocker(self._mutex):
                rec = self._recording

            if rec and not self._in_blue and not self._in_black:
                # Wall-clock timestamp sync check
                elapsed = now - self._start_time
                expected_frames = int(round(elapsed * loop["fps"]))
                lag = expected_frames - (self._frames_written + 1)

                # If capture card dropped frames due to tracking hitch, fill with duplicate frames
                if lag > 0 and self._last_written_frame is not None:
                    fill = min(lag, 5)
                    for _ in range(fill):
                        self._writer_thread.push(self._last_written_frame)
                        self._frames_written += 1
                        self._frames_duplicated += 1

                self._writer_thread.push(frame)
                self._frames_written += 1
                self._last_written_frame = frame

            # ── Timecode and sync status ──────────────────────
            if now - last_timecode >= timecode_interval:
                last_timecode = now
                elapsed = now - self._start_time if self._start_time else 0.0
                self.timecode_tick.emit(elapsed)
                self.sync_stats.emit(self._frames_written, self._frames_duplicated)

        cap.release()
        self._cap = None

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

    def clear(self):
        self.events.clear()


# ─────────────────────────────────────────────
#  Camera picker dialog
# ─────────────────────────────────────────────

def _probe_camera(index: int) -> dict | None:
    PROBE_RESOLUTIONS = [
        (1920, 1080),
        (1280,  720),
        ( 720,  576),  # PAL
        ( 720,  480),  # NTSC
        ( 640,  480),
        ( 320,  240),
    ]
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened() and backend != cv2.CAP_ANY:
        cap = cv2.VideoCapture(index)
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
    from concurrent.futures import ThreadPoolExecutor, as_completed
    found = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(_probe_camera, i): i for i in range(max_index)}
        for fut in as_completed(futures):
            info = fut.result()
            if info:
                found.append(info)
    return found


class CameraPickerDialog(QDialog):
    _scan_done = pyqtSignal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select camera")
        self.setMinimumWidth(460)
        _icon_path = resource_path("vhs_capture.ico")
        if os.path.exists(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))
        self.setStyleSheet(
            f"QDialog{{background:#1a1a1a;color:#e8e8e8}}"
            f"QLabel{{color:#e8e8e8}}"
        )
        self.selected_index = 0
        self.selected_w = 720
        self.selected_h = 576
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
#  Style constants & Widgets
# ─────────────────────────────────────────────

BG       = "#0f0f0f"
SURFACE  = "#1a1a1a"
BORDER   = "#2a2a2a"
TEXT     = "#e8e8e8"
MUTED    = "#888888"
BLUE     = "#378add"
BLUE_BG  = "#0c2a42"
GREEN    = "#639922"
GREEN_BG = "#1a2e0a"
RED      = "#e24b4a"
RED_BG   = "#2e0a0a"
AMBER    = "#ba7517"
AMBER_BG = "#2e1f0a"

def _pill(text: str, fg: str, bg: str) -> QLabel:
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
#  Main Window
# ─────────────────────────────────────────────

class _AudioScanner(QThread):
    done = pyqtSignal(list)
    def run(self):
        ff = _system_ffmpeg() or FFMPEG_BIN
        devs = list_audio_devices(ff)
        self.done.emit(devs)


class VHSCapture(QMainWindow):
    _clip_size_updated = pyqtSignal(object, str, float)
    _clip_purged       = pyqtSignal(str, str)  # real_path, reason

    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"VHS Capture v{__version__}")
        self.setMinimumSize(960, 620)

        _icon_path = resource_path("vhs_capture.ico")
        if os.path.exists(_icon_path):
            self.setWindowIcon(QIcon(_icon_path))

        self._capture: CaptureThread | None = None
        self._audio_meter: AudioMeterThread | None = None
        self._recording = False
        self._log = EventLog()
        self._clips: dict[str, QListWidgetItem] = {}
        self._segment_records: list[tuple[str, float]] = []  # (filename, duration_sec)
        self._seg_count = 0
        self._in_blue = False
        self._in_black = False
        self._camera_index = 0
        self._camera_native_w = 720
        self._camera_native_h = 576
        self._camera_native_fps = 25.0
        self._last_preview_frame: np.ndarray | None = None

        cfg = load_settings()
        self._output_dir = cfg.get("output_dir", str(Path.home() / "VHS_Captures"))

        # Clean up transient temp files
        try:
            out_p = Path(self._output_dir)
            if out_p.is_dir():
                for tmp_file in out_p.glob(".audio_tmp_*.pcm"):
                    tmp_file.unlink(missing_ok=True)
                for tmp_file in out_p.glob("*.mux_tmp*"):
                    tmp_file.unlink(missing_ok=True)
        except Exception:
            pass

        self._clip_size_updated.connect(self._update_clip_item)
        self._clip_purged.connect(self._on_clip_purged)
        self._build_ui()
        self._apply_windows_theme()
        self._restore_settings(cfg)
        self._setup_shortcuts()

        # Disk space monitor timer
        self._disk_timer = QTimer(self)
        self._disk_timer.timeout.connect(self._check_disk_space)
        self._disk_timer.start(3000)
        self._check_disk_space()

        self._log_event("info", f"VHS Capture v{__version__} ready",
                        "Shortcuts: Space=Rec, Enter=Split, Ctrl+S=Snapshot, Ctrl+O=Camera")

    def _setup_shortcuts(self):
        QShortcut(QKeySequence("Space"), self, self._toggle_record)
        QShortcut(QKeySequence("Return"), self, self._manual_split)
        QShortcut(QKeySequence("Enter"), self, self._manual_split)
        QShortcut(QKeySequence("Ctrl+O"), self, self._open_camera)
        QShortcut(QKeySequence("Ctrl+S"), self, self._take_snapshot)
        QShortcut(QKeySequence("F1"), self, self._show_about_dialog)
        QShortcut(QKeySequence("Ctrl+I"), self, self._show_about_dialog)
        QShortcut(QKeySequence("Ctrl+P"), self, self._manage_profiles_dialog)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, self._save_profile_dialog)

    def _make_btn(self, text, primary=False, danger=False):
        b = QPushButton(text)
        b.setFixedHeight(36)
        base = f"border:1px solid {BORDER};border-radius:8px;padding:0 14px;font-size:13px;font-weight:500;"
        if primary:
            b.setStyleSheet(
                f"QPushButton{{{base}background:{SURFACE};color:{TEXT};}}"
                f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff;}}"
                f"QPushButton:pressed{{background:#1d2028;}}"
                f"QPushButton:disabled{{color:#444;border-color:{BORDER};}}"
            )
        elif danger:
            b.setStyleSheet(
                f"QPushButton{{{base}background:{RED_BG};color:{RED};border-color:{RED};}}"
                f"QPushButton:hover{{background:#3d1012;border-color:#ff4d4d;color:#fff;}}"
                f"QPushButton:pressed{{background:#280808;}}"
                f"QPushButton:disabled{{color:#444;border-color:{BORDER};}}"
            )
        else:
            b.setStyleSheet(
                f"QPushButton{{{base}background:transparent;color:{MUTED};}}"
                f"QPushButton:hover{{background:{SURFACE};color:{TEXT};border-color:#444;}}"
                f"QPushButton:pressed{{background:#181a20;}}"
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
        val_lbl.setStyleSheet(f"color:{TEXT};font-size:13px;font-weight:500;min-width:44px;text-align:right")
        val_lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        sl.valueChanged.connect(lambda v: val_lbl.setText(fmt_fn(v)))
        row.addWidget(lbl)
        row.addWidget(sl, 1)
        row.addWidget(val_lbl)
        return row, sl, val_lbl

    def _section(self, title):
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
        hdr_icon = QLabel()
        _icon_path = resource_path("vhs_capture.ico")
        if os.path.exists(_icon_path):
            hdr_icon.setPixmap(QIcon(_icon_path).pixmap(38, 38))
            hdr_icon.setFixedSize(38, 38)
            hdr_row.addWidget(hdr_icon)
            hdr_row.addSpacing(6)
        title = QLabel("VHS Capture")
        title.setStyleSheet(f"color:{TEXT};font-size:19px;font-weight:700;letter-spacing:0.02em")
        sub = QLabel("Analog tape digitizer · A/V sync protection · auto-split")
        sub.setStyleSheet(f"color:{MUTED};font-size:12px")
        hdr_col = QVBoxLayout()
        hdr_col.setSpacing(2)
        hdr_col.addWidget(title)
        hdr_col.addWidget(sub)
        hdr_row.addLayout(hdr_col)
        hdr_row.addStretch()

        # Preset / Profile selector
        lbl_prof = QLabel("Profile:")
        lbl_prof.setStyleSheet(f"color:{MUTED};font-size:11px;font-weight:600")
        hdr_row.addWidget(lbl_prof)

        self.combo_profile = QComboBox()
        self.combo_profile.setToolTip("Select a predefined hardware preset or custom saved profile")
        self.combo_profile.setFixedHeight(24)
        self.combo_profile.setMinimumWidth(180)
        self.combo_profile.setStyleSheet(
            f"QComboBox{{background:{SURFACE};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:11px;font-weight:500;padding:2px 8px}}"
            f"QComboBox:hover{{border-color:#454c5e}}"
            f"QComboBox QAbstractItemView{{background:{SURFACE};border:1px solid {BORDER};"
            f"color:{TEXT};selection-background-color:#2a3a55}}"
        )
        self._refresh_profile_list()
        self.combo_profile.activated.connect(self._on_profile_activated)
        hdr_row.addWidget(self.combo_profile)

        self.btn_save_profile = QPushButton("💾 Save")
        self.btn_save_profile.setToolTip("Save current settings as a profile (Ctrl+Shift+S)")
        self.btn_save_profile.setFixedHeight(24)
        self.btn_save_profile.setStyleSheet(
            f"QPushButton{{color:{TEXT};background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:6px;padding:2px 8px;font-size:11px;font-weight:600}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
        )
        self.btn_save_profile.clicked.connect(self._save_profile_dialog)
        hdr_row.addWidget(self.btn_save_profile)

        self.btn_manage_profile = QPushButton("⚙")
        self.btn_manage_profile.setToolTip("Manage saved profiles (Delete / Explore)")
        self.btn_manage_profile.setFixedHeight(24)
        self.btn_manage_profile.setFixedWidth(24)
        self.btn_manage_profile.setStyleSheet(
            f"QPushButton{{color:{MUTED};background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:6px;font-size:12px;font-weight:600}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
        )
        self.btn_manage_profile.clicked.connect(self._manage_profiles_dialog)
        hdr_row.addWidget(self.btn_manage_profile)

        self.lbl_disk_header = QLabel("💾 — GB free")
        self.lbl_disk_header.setStyleSheet(
            f"color:{MUTED};background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:10px;padding:2px 10px;font-size:11px;font-weight:600"
        )
        hdr_row.addWidget(self.lbl_disk_header)

        self.btn_about = QPushButton("ℹ Info")
        self.btn_about.setToolTip("System & Version Information (F1)")
        self.btn_about.setFixedHeight(24)
        self.btn_about.setStyleSheet(
            f"QPushButton{{color:{MUTED};background:{SURFACE};border:1px solid {BORDER};"
            f"border-radius:10px;padding:2px 10px;font-size:11px;font-weight:600}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
        )
        self.btn_about.clicked.connect(self._show_about_dialog)
        hdr_row.addWidget(self.btn_about)

        left.addLayout(hdr_row)

        # Video preview card
        preview_card = _card()
        preview_card.setStyleSheet(
            f"QFrame{{background:#000;border:1px solid {BORDER};border-radius:10px;}}"
        )
        preview_stack = QVBoxLayout(preview_card)
        preview_stack.setContentsMargins(0, 0, 0, 0)
        preview_stack.setSpacing(0)

        self.preview_label = QLabel()
        self.preview_label.setMinimumHeight(240)
        self.preview_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setStyleSheet("background:#000;border-radius:10px")
        self.preview_label.setText(f'<span style="color:{MUTED};font-size:14px">No camera — click Open camera</span>')
        preview_stack.addWidget(self.preview_label, 1)

        # Badge overlay row
        overlay_row = QHBoxLayout()
        overlay_row.setContentsMargins(10, 0, 10, 10)
        overlay_row.setSpacing(6)
        self.badge_idle   = _pill("● Idle",         MUTED, "#1a1a1a")
        self.badge_rec    = _pill("● REC",          "#fff", RED)
        self.badge_blue   = _pill("◆ Blue screen",  "#fff", BLUE)
        self.badge_black  = _pill("▪ Blank/Black",  "#fff", "#444444")
        self.badge_static = _pill("░ Static Noise", "#fff", AMBER)
        self.badge_rec.setVisible(False)
        self.badge_blue.setVisible(False)
        self.badge_black.setVisible(False)
        self.badge_static.setVisible(False)

        overlay_row.addWidget(self.badge_idle)
        overlay_row.addWidget(self.badge_rec)
        overlay_row.addWidget(self.badge_blue)
        overlay_row.addWidget(self.badge_black)
        overlay_row.addWidget(self.badge_static)
        overlay_row.addStretch()

        self.lbl_timecode = QLabel("0:00:00")
        self.lbl_timecode.setStyleSheet(
            f"color:{TEXT};background:#1a1a1a;border:1px solid {BORDER};"
            f"border-radius:10px;padding:2px 10px;font-size:11px;font-family:monospace;font-weight:600"
        )
        overlay_row.addWidget(self.lbl_timecode)
        preview_stack.addLayout(overlay_row)
        left.addWidget(preview_card, 1)

        # Meters row
        meters_row = QHBoxLayout()
        meters_row.setSpacing(8)

        blue_lbl = QLabel("Blue:")
        blue_lbl.setStyleSheet(f"color:{MUTED};font-size:12px;min-width:32px")
        self.blue_bar = QProgressBar()
        self.blue_bar.setRange(0, 100)
        self.blue_bar.setValue(0)
        self.blue_bar.setTextVisible(False)
        self.blue_bar.setFixedHeight(8)
        self.blue_bar.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border-radius:4px;border:none}}"
            f"QProgressBar::chunk{{background:{BLUE};border-radius:4px}}"
        )

        audio_lbl = QLabel("Audio:")
        audio_lbl.setStyleSheet(f"color:{MUTED};font-size:12px;min-width:38px")
        self.audio_bar = QProgressBar()
        self.audio_bar.setRange(0, 100)
        self.audio_bar.setValue(0)
        self.audio_bar.setTextVisible(False)
        self.audio_bar.setFixedHeight(14)
        self.audio_bar.setStyleSheet(
            f"QProgressBar{{background:{BORDER};border-radius:4px;border:none}}"
            f"QProgressBar::chunk{{background:{GREEN};border-radius:4px}}"
        )

        meters_row.addWidget(blue_lbl)
        meters_row.addWidget(self.blue_bar, 1)
        meters_row.addSpacing(10)
        meters_row.addWidget(audio_lbl)
        meters_row.addWidget(self.audio_bar, 1)
        left.addLayout(meters_row)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self.btn_cam      = self._make_btn("📹  Open camera", primary=True)
        self.btn_settings = self._make_btn("⚙️  Device settings")
        self.btn_rec      = self._make_btn("⏺  Start recording", primary=True)
        self.btn_split    = self._make_btn("✂  Split now")
        self.btn_snapshot = self._make_btn("📸  Snapshot")

        self.btn_cam.clicked.connect(self._open_camera)
        self.btn_settings.clicked.connect(self._open_device_settings)
        self.btn_rec.clicked.connect(self._toggle_record)
        self.btn_split.clicked.connect(self._manual_split)
        self.btn_snapshot.clicked.connect(self._take_snapshot)

        self.btn_settings.setEnabled(False)
        self.btn_rec.setEnabled(False)
        self.btn_split.setEnabled(False)
        self.btn_snapshot.setEnabled(False)

        btn_row.addWidget(self.btn_cam)
        btn_row.addWidget(self.btn_settings)
        btn_row.addWidget(self.btn_rec)
        btn_row.addWidget(self.btn_split)
        btn_row.addWidget(self.btn_snapshot)
        left.addLayout(btn_row)

        # Stats cards row (4 cards: Time, Clips, Splits, Sync drops)
        stat_row = QHBoxLayout()
        stat_row.setSpacing(10)
        for attr, title_txt, sub_txt in [
            ("stat_time",   "0:00:00",  "recording time"),
            ("stat_clips",  "0",        "clips saved"),
            ("stat_splits", "0",        "auto-splits"),
            ("stat_sync",   "100% sync", "drop protection"),
        ]:
            card = _card()
            lay = QVBoxLayout(card)
            lay.setContentsMargins(12, 10, 12, 10)
            lay.setSpacing(2)
            val = QLabel(title_txt)
            val.setStyleSheet(f"color:{TEXT};font-size:20px;font-weight:600")
            sub = QLabel(sub_txt)
            sub.setStyleSheet(f"color:{MUTED};font-size:11px")
            lay.addWidget(val)
            lay.addWidget(sub)
            setattr(self, attr, val)
            stat_row.addWidget(card, 1)
        left.addLayout(stat_row)
        left.addStretch()

        root.addLayout(left, 3)

        # ── RIGHT COLUMN (ScrollArea) ───────────────────────
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

        # 1. Output section
        out_card, out_lay = self._section("OUTPUT")
        dir_row = QHBoxLayout()
        self.lbl_outdir = QLabel(self._output_dir)
        self.lbl_outdir.setStyleSheet(f"color:{TEXT};font-size:12px")
        self.lbl_outdir.setWordWrap(True)
        btn_dir = self._make_btn("📁  Browse…")
        btn_dir.setFixedWidth(100)
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

        ts_now = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.preview_fname_lbl = QLabel(f"→  clip01_{ts_now}.mp4")
        self.preview_fname_lbl.setStyleSheet("color:#aaa;font-size:11px;font-family:Consolas,'Courier New',monospace")
        out_lay.addWidget(self.preview_fname_lbl)

        def _update_fname_preview():
            t = self.edit_title.text().strip()
            p = self.edit_part.text().strip()
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            enc = self._selected_encoder_info()
            ext = "mkv" if (enc and enc["name"] == "ffv1") else "mp4"
            parts = [x for x in [t, p, "clip01", ts] if x]
            self.preview_fname_lbl.setText(f"→  {'_'.join(parts)}.{ext}")

        self.edit_title.textChanged.connect(_update_fname_preview)
        self.edit_part.textChanged.connect(_update_fname_preview)

        # 2. Capture Settings (VHS Standards)
        cap_card, cap_lay = self._section("CAPTURE SETTINGS (VHS STANDARDS)")

        self.combo_res = QComboBox()
        self.combo_res.addItems([
            "720×576 (PAL 4:3)",
            "720×480 (NTSC 4:3)",
            "1280×720 (HD 16:9)",
            "1920×1080 (Full HD)",
            "640×480 (VGA 4:3)",
        ])
        self.combo_res.setCurrentIndex(0)  # Default PAL 576
        self.combo_res.currentIndexChanged.connect(self._on_res_changed)
        cap_lay.addLayout(self._settings_row("Resolution:", self.combo_res))

        self.spin_fps = QDoubleSpinBox()
        self.spin_fps.setRange(10.0, 60.0)
        self.spin_fps.setDecimals(2)
        self.spin_fps.setValue(25.0)
        cap_lay.addLayout(self._settings_row("FPS:", self.spin_fps))

        self.combo_aspect = QComboBox()
        self.combo_aspect.addItems(["4:3 (Standard VHS)", "16:9 (Widescreen anamorphic)"])
        cap_lay.addLayout(self._settings_row("Aspect Ratio:", self.combo_aspect))

        self.combo_deint = QComboBox()
        self.combo_deint.addItems([
            "Off (Pass-through)",
            "bwdif (Standard 25/30p)",
            "bwdif 2x (Smooth 50/60p motion)",
            "yadif (Fast 25/30p)",
        ])
        self.combo_deint.setCurrentIndex(1)  # Default bwdif on
        cap_lay.addLayout(self._settings_row("Deinterlace:", self.combo_deint))

        self.chk_mask_head = QCheckBox("Mask head-switching noise (bottom 12px)")
        self.chk_mask_head.setChecked(True)
        self.chk_mask_head.setStyleSheet(f"color:{MUTED};font-size:13px")
        cap_lay.addWidget(self.chk_mask_head)
        right.addWidget(cap_card)

        # 3. Audio Settings
        audio_card, audio_lay = self._section("AUDIO CONTROLS")
        self.combo_audio = QComboBox()
        self.combo_audio.addItem("Scanning…")
        self.combo_audio.setEnabled(False)
        self._audio_devices = []

        audio_row = QHBoxLayout()
        audio_lbl = QLabel("Audio device:")
        audio_lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:120px")
        btn_audio_refresh = self._make_btn("🔄  Rescan")
        btn_audio_refresh.setFixedWidth(88)
        btn_audio_refresh.setFixedHeight(30)
        btn_audio_refresh.clicked.connect(self._rescan_audio)
        self.combo_audio.currentIndexChanged.connect(self._on_audio_device_changed)
        audio_row.addWidget(audio_lbl)
        audio_row.addWidget(self.combo_audio, 1)
        audio_row.addWidget(btn_audio_refresh)
        audio_lay.addLayout(audio_row)

        self.combo_audio_channels = QComboBox()
        self.combo_audio_channels.addItems([
            "Stereo (Normal)",
            "Mono (Duplicate Left to both)",
            "Mono (Duplicate Right to both)",
        ])
        audio_lay.addLayout(self._settings_row("Channels:", self.combo_audio_channels))

        self.chk_hum_filter = QCheckBox("Mains hum filter (50Hz / 60Hz highpass)")
        self.chk_hum_filter.setChecked(True)
        self.chk_hum_filter.setStyleSheet(f"color:{MUTED};font-size:13px")
        audio_lay.addWidget(self.chk_hum_filter)

        self.chk_normalize = QCheckBox("Normalize audio levels (EBU R128 loudnorm)")
        self.chk_normalize.setChecked(False)
        self.chk_normalize.setStyleSheet(f"color:{MUTED};font-size:13px")
        audio_lay.addWidget(self.chk_normalize)
        right.addWidget(audio_card)

        # 4. Encoder & Quality
        enc_card, enc_lay = self._section("ENCODER & QUALITY")
        self.combo_enc = QComboBox()
        self.combo_enc.addItem("Scanning encoders…")
        self.combo_enc.setEnabled(False)
        self.combo_enc.currentIndexChanged.connect(_update_fname_preview)
        enc_lay.addLayout(self._settings_row("Encoder:", self.combo_enc))

        self._encoder_list: list[dict] = []
        class _EncoderSignal(QThread):
            done = pyqtSignal(list, list)
            def run(self_inner):
                working, failed = probe_encoders()
                self._encoder_list = working
                self_inner.done.emit(working, failed)
        self._enc_probe = _EncoderSignal()
        self._enc_probe.done.connect(self._populate_encoders)
        self._enc_probe.start()

        crf_row, self.sl_crf, _ = self._make_slider_row(
            "CRF (Quality)", 15, 45, 28,
            lambda v: f"{v}  {'Archive' if v<=18 else 'High' if v<=24 else 'Balanced' if v<=30 else 'Preview'}"
        )
        enc_lay.addLayout(crf_row)
        crf_hint = QLabel("15=Archive  24=High  28=Balanced  38=Preview  (Ignored for FFV1)")
        crf_hint.setStyleSheet(f"color:{MUTED};font-size:11px")
        enc_lay.addWidget(crf_hint)
        right.addWidget(enc_card)

        # 5. Smart Detection & Auto-Split
        det_card, det_lay = self._section("DETECTION & AUTO-SPLIT")
        thresh_row, self.sl_thresh, _ = self._make_slider_row(
            "Blue threshold", 20, 90, 60, lambda v: f"{v}%")
        det_lay.addLayout(thresh_row)

        dur_row, self.sl_mindur, _ = self._make_slider_row(
            "Min blue duration", 3, 90, 15, lambda v: f"{v/10:.1f}s")
        det_lay.addLayout(dur_row)

        self.chk_auto_split_blue = QCheckBox("Auto-split on blue screen")
        self.chk_auto_split_blue.setChecked(True)
        self.chk_auto_split_blue.setStyleSheet(f"color:{MUTED};font-size:13px")
        det_lay.addWidget(self.chk_auto_split_blue)

        self.chk_auto_split_black = QCheckBox("Auto-split on blank black screen (e.g. cut marks)")
        self.chk_auto_split_black.setChecked(False)
        self.chk_auto_split_black.setStyleSheet(f"color:{MUTED};font-size:13px")
        det_lay.addWidget(self.chk_auto_split_black)

        self.chk_auto_stop_tape_end = QCheckBox("Auto-stop on Tape End (blank screen + silence)")
        self.chk_auto_stop_tape_end.setChecked(True)
        self.chk_auto_stop_tape_end.setStyleSheet(f"color:{MUTED};font-size:13px")
        det_lay.addWidget(self.chk_auto_stop_tape_end)

        tape_end_row = QHBoxLayout()
        tape_end_lbl = QLabel("End timeout:")
        tape_end_lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:100px")
        self.spin_tape_end_timeout = QSpinBox()
        self.spin_tape_end_timeout.setRange(10, 180)
        self.spin_tape_end_timeout.setValue(30)
        self.spin_tape_end_timeout.setSuffix(" sec")
        INPUT_STYLE = (
            f"background:{BG};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:13px;padding:3px 8px"
        )
        self.spin_tape_end_timeout.setStyleSheet(INPUT_STYLE)
        tape_end_row.addWidget(tape_end_lbl)
        tape_end_row.addWidget(self.spin_tape_end_timeout)
        tape_end_row.addStretch()
        det_lay.addLayout(tape_end_row)

        self.chk_purge_empty = QCheckBox("Auto-purge empty/blank clips when recording stops")
        self.chk_purge_empty.setChecked(True)
        self.chk_purge_empty.setToolTip(
            "Detects and deletes clips that contain only blank/blue/static frames or zero usable duration after recording stops."
        )
        self.chk_purge_empty.setStyleSheet(f"color:{MUTED};font-size:13px")
        det_lay.addWidget(self.chk_purge_empty)
        right.addWidget(det_card)

        # 6. Tape Timer
        timer_card, timer_lay = self._section("TAPE TIMER")
        self.chk_tape_timer = QCheckBox("Auto-stop after tape length")
        self.chk_tape_timer.setChecked(False)
        self.chk_tape_timer.setStyleSheet(f"color:{MUTED};font-size:13px")
        timer_lay.addWidget(self.chk_tape_timer)

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

        custom_row = QHBoxLayout()
        custom_lbl = QLabel("Custom (min):")
        custom_lbl.setStyleSheet(f"color:{MUTED};font-size:13px;min-width:100px")
        self.spin_tape_mins = QSpinBox()
        self.spin_tape_mins.setRange(1, 360)
        self.spin_tape_mins.setValue(60)
        self.spin_tape_mins.setSuffix(" min")
        self.spin_tape_mins.setStyleSheet(INPUT_STYLE)
        self.spin_tape_mins.setFixedHeight(30)
        self.spin_tape_mins.valueChanged.connect(self._sync_tape_preset_btns)
        custom_row.addWidget(custom_lbl)
        custom_row.addWidget(self.spin_tape_mins)
        custom_row.addStretch()
        timer_lay.addLayout(custom_row)

        self.lbl_tape_remaining = QLabel("—")
        self.lbl_tape_remaining.setStyleSheet(f"color:{MUTED};font-size:12px;font-family:monospace")
        timer_lay.addWidget(self.lbl_tape_remaining)
        right.addWidget(timer_card)

        # Output card placement
        right.addWidget(out_card)

        # 7. Saved Clips & Chapters Export
        clips_card, clips_lay = self._section("SAVED CLIPS")
        self.clips_list = QListWidget()
        self.clips_list.setStyleSheet(
            f"QListWidget{{background:{BG};border:none;color:{TEXT};font-size:12px}}"
            f"QListWidget::item{{padding:5px 4px;border-bottom:1px solid {BORDER}}}"
            f"QListWidget::item:selected{{background:{BLUE_BG};color:#fff}}"
        )
        self.clips_list.setMinimumHeight(90)
        self.clips_list.setMaximumHeight(140)
        self.clips_list.itemDoubleClicked.connect(self._open_clip_folder)
        clips_lay.addWidget(self.clips_list)

        clips_btn_row = QHBoxLayout()
        clips_hint = QLabel("Double-click to open folder")
        clips_hint.setStyleSheet(f"color:{MUTED};font-size:11px")
        btn_edl = self._make_btn("📑  Export EDL & Chapters")
        btn_edl.setFixedHeight(26)
        btn_edl.clicked.connect(self._export_edl_and_chapters)
        btn_purge_empty = self._make_btn("🗑️  Purge empty")
        btn_purge_empty.setFixedHeight(26)
        btn_purge_empty.setToolTip("Scan current saved clips and delete any clips with no content or blank/empty frames")
        btn_purge_empty.clicked.connect(self._purge_empty_clips_manual)
        clips_btn_row.addWidget(clips_hint, 1)
        clips_btn_row.addWidget(btn_purge_empty)
        clips_btn_row.addWidget(btn_edl)
        clips_lay.addLayout(clips_btn_row)
        right.addWidget(clips_card)

        # 8. Event log
        log_card, log_lay = self._section("EVENT LOG")
        log_hdr_row = QHBoxLayout()
        log_hdr_row.addStretch()
        btn_clear_log = self._make_btn("Clear")
        btn_clear_log.setFixedWidth(60)
        btn_clear_log.setFixedHeight(26)
        btn_clear_log.clicked.connect(self._clear_log)
        log_hdr_row.addWidget(btn_clear_log)
        log_lay.addLayout(log_hdr_row)

        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(100)
        self.log_view.setStyleSheet(
            f"background:{BG};color:{TEXT};font-family:monospace;font-size:12px;"
            f"border:none;border-radius:0"
        )
        log_lay.addWidget(self.log_view)
        right.addWidget(log_card, 1)

        root.addWidget(right_scroll, 2)
        self._rescan_audio()

    def _on_res_changed(self, idx: int):
        # Auto-configure standard framerate when selecting PAL vs NTSC
        if idx == 0:  # 720x576 PAL
            self.spin_fps.setValue(25.0)
            self.combo_aspect.setCurrentIndex(0)
        elif idx == 1:  # 720x480 NTSC
            self.spin_fps.setValue(29.97)
            self.combo_aspect.setCurrentIndex(0)
        elif idx == 2:  # 1280x720
            self.combo_aspect.setCurrentIndex(1)
        elif idx == 3:  # 1920x1080
            self.combo_aspect.setCurrentIndex(1)
        elif idx == 4:  # 640x480
            self.combo_aspect.setCurrentIndex(0)

    def _check_disk_space(self):
        try:
            total, used, free = shutil.disk_usage(self._output_dir)
            free_gb = free / (1024 ** 3)
            # Estimate remaining hours (~3.5 GB/hr for modern encoding, ~12 GB/hr for FFV1)
            is_ffv1 = (self._selected_encoder_info() or {}).get("name") == "ffv1"
            rate = 12.0 if is_ffv1 else 3.5
            est_hours = free_gb / rate

            status_str = f"💾 {free_gb:.1f} GB free (~{est_hours:.0f}h)"
            self.lbl_disk_header.setText(status_str)

            if free_gb < 1.5 and self._recording:
                self._log_event("error", "CRITICAL DISK SPACE (<1.5 GB)", "Stopping recording safely to prevent data loss")
                self._stop_recording()
        except Exception:
            pass

    def _populate_encoders(self, working: list[dict], failed: list[dict] = None):
        failed = failed or []
        self.combo_enc.clear()
        if not working:
            self.combo_enc.addItem("No encoders found — check FFmpeg install")
        else:
            for e in working:
                self.combo_enc.addItem(e["label"])
            self.combo_enc.setEnabled(True)

        saved = load_settings().get("encoder", "")
        restored = False
        for i, e in enumerate(working):
            if e["name"] == saved:
                self.combo_enc.setCurrentIndex(i)
                restored = True
                break

        if not restored:
            hw = [e for e in working if e["is_hw"]]
            if hw:
                self.combo_enc.setCurrentIndex(working.index(hw[0]))

        hw_working = [e for e in working if e["is_hw"]]
        if hw_working:
            best = hw_working[0]
            self._log_event("info", f"GPU encoding active: {best['label']}",
                            f"{len(hw_working)} GPU encoder(s) available")
        else:
            self._log_event("info", "CPU encoding active", "No GPU acceleration detected")

    def _populate_audio_devices(self, devices: list[str]):
        self.combo_audio.clear()
        self.combo_audio.addItem("No audio")
        for d in devices:
            self.combo_audio.addItem(d)
        self.combo_audio.setEnabled(True)

        saved = load_settings().get("audio_device", "")
        if saved:
            for i in range(self.combo_audio.count()):
                if self.combo_audio.itemText(i) == saved:
                    self.combo_audio.setCurrentIndex(i)
                    break
        elif len(devices) == 1:
            self.combo_audio.setCurrentIndex(1)

        if devices:
            self._log_event("info", f"Audio devices found: {len(devices)} device(s)", "")

    def _rescan_audio(self):
        self.combo_audio.clear()
        self.combo_audio.addItem("Scanning…")
        self.combo_audio.setEnabled(False)
        if hasattr(self, "_audio_scanner") and self._audio_scanner and self._audio_scanner.isRunning():
            self._audio_scanner.quit()
            self._audio_scanner.wait(1000)
        self._audio_scanner = _AudioScanner()
        self._audio_scanner.done.connect(self._populate_audio_devices)
        self._audio_scanner.start()

    def _on_audio_device_changed(self, idx: int):
        if self._audio_meter:
            self._audio_meter.stop()
            self._audio_meter = None
        if _AUDIO_AVAILABLE and idx > 0 and self._capture:
            dev_name = self.combo_audio.currentText()
            self._audio_meter = AudioMeterThread(dev_name)
            self._audio_meter.level.connect(
                lambda v: self.audio_bar.setValue(int(v * 100))
            )
            self._audio_meter.level.connect(self._capture.set_audio_level)
            self._audio_meter.start()
        elif idx <= 0:
            self.audio_bar.setValue(0)

    def _selected_encoder_info(self) -> dict | None:
        idx = self.combo_enc.currentIndex()
        if self._encoder_list and 0 <= idx < len(self._encoder_list):
            return self._encoder_list[idx]
        return None

    def _get_config(self):
        res_text = self.combo_res.currentText().split()[0]
        w, h = (int(x) for x in res_text.split("×"))
        info = self._selected_encoder_info()
        enc  = info["name"] if info else "libsvtav1"

        deint_val = "off"
        d_idx = self.combo_deint.currentIndex()
        if d_idx == 1:
            deint_val = "bwdif"
        elif d_idx == 2:
            deint_val = "bwdif_2x"
        elif d_idx == 3:
            deint_val = "yadif"

        aspect_val = "4:3" if self.combo_aspect.currentIndex() == 0 else "16:9"

        channel_mode = "stereo"
        if self.combo_audio_channels.currentIndex() == 1:
            channel_mode = "mono_left"
        elif self.combo_audio_channels.currentIndex() == 2:
            channel_mode = "mono_right"

        return {
            "camera_index":        self._camera_index,
            "width":               w,
            "height":              h,
            "fps":                 self.spin_fps.value(),
            "encoder":             enc,
            "encoder_info":        info,
            "crf":                 self.sl_crf.value(),
            "aspect_ratio":        aspect_val,
            "deinterlace":         deint_val,
            "mask_head_switching": self.chk_mask_head.isChecked(),
            "blue_threshold":      self.sl_thresh.value() / 100,
            "min_blue_seconds":    self.sl_mindur.value() / 10,
            "auto_split_blue":     self.chk_auto_split_blue.isChecked(),
            "auto_split_black":    self.chk_auto_split_black.isChecked(),
            "auto_stop_tape_end":  self.chk_auto_stop_tape_end.isChecked(),
            "tape_end_timeout":    float(self.spin_tape_end_timeout.value()),
            "purge_empty_clips":   self.chk_purge_empty.isChecked(),
            "output_dir":          self._output_dir,
            "title_prefix":        self.edit_title.text().strip(),
            "part_prefix":         self.edit_part.text().strip(),
            "audio_device":        "" if self.combo_audio.currentIndex() == 0 else self.combo_audio.currentText(),
            "audio_channel_mode":  channel_mode,
            "audio_hum_filter":    self.chk_hum_filter.isChecked(),
            "audio_normalize":     self.chk_normalize.isChecked(),
        }

    def _current_settings(self) -> dict:
        cur_prof = ""
        if hasattr(self, "combo_profile") and self.combo_profile.currentIndex() >= 0:
            cur_prof = self.combo_profile.currentData() or ""
        return {
            **self._get_config(),
            "resolution":         self.combo_res.currentText(),
            "tape_timer_enabled": self.chk_tape_timer.isChecked(),
            "tape_timer_mins":    self.spin_tape_mins.value(),
            "active_profile":     cur_prof,
        }

    def _restore_settings(self, cfg: dict):
        if "output_dir" in cfg:
            self._output_dir = cfg["output_dir"]
        self.lbl_outdir.setText(self._output_dir)
        if "title_prefix" in cfg:
            self.edit_title.setText(cfg.get("title_prefix", ""))
        if "part_prefix" in cfg:
            self.edit_part.setText(cfg.get("part_prefix", ""))

        if "resolution" in cfg:
            saved_res = cfg.get("resolution", "")
            idx = self.combo_res.findText(saved_res)
            if idx >= 0:
                self.combo_res.setCurrentIndex(idx)

        if "fps" in cfg:
            self.spin_fps.setValue(float(cfg.get("fps", 25.0)))
        if "crf" in cfg:
            self.sl_crf.setValue(int(cfg.get("crf", 28)))

        if "aspect_ratio" in cfg:
            self.combo_aspect.setCurrentIndex(1 if cfg.get("aspect_ratio") == "16:9" else 0)

        if "deinterlace" in cfg:
            d_val = cfg.get("deinterlace", "")
            for i in range(self.combo_deint.count()):
                if d_val.lower() in self.combo_deint.itemText(i).lower():
                    self.combo_deint.setCurrentIndex(i)
                    break

        if "encoder" in cfg:
            enc = cfg.get("encoder", "")
            for i in range(self.combo_enc.count()):
                if enc.lower() in self.combo_enc.itemText(i).lower():
                    self.combo_enc.setCurrentIndex(i)
                    break

        if "audio_channel_mode" in cfg:
            m = cfg.get("audio_channel_mode", "stereo")
            if m == "mono_left":
                self.combo_audio_channels.setCurrentIndex(1)
            elif m == "mono_right":
                self.combo_audio_channels.setCurrentIndex(2)
            else:
                self.combo_audio_channels.setCurrentIndex(0)

        if "blue_threshold" in cfg:
            self.sl_thresh.setValue(int(cfg.get("blue_threshold", 0.60) * 100))
        if "min_blue_seconds" in cfg:
            self.sl_mindur.setValue(int(cfg.get("min_blue_seconds", 1.5) * 10))
        if "mask_head_switching" in cfg:
            self.chk_mask_head.setChecked(cfg.get("mask_head_switching", True))
        if "audio_hum_filter" in cfg:
            self.chk_hum_filter.setChecked(cfg.get("audio_hum_filter", True))
        if "audio_normalize" in cfg:
            self.chk_normalize.setChecked(cfg.get("audio_normalize", False))
        if "auto_split_blue" in cfg:
            self.chk_auto_split_blue.setChecked(cfg.get("auto_split_blue", True))
        if "auto_split_black" in cfg:
            self.chk_auto_split_black.setChecked(cfg.get("auto_split_black", False))
        if "auto_stop_tape_end" in cfg:
            self.chk_auto_stop_tape_end.setChecked(cfg.get("auto_stop_tape_end", True))
        if "tape_end_timeout" in cfg:
            self.spin_tape_end_timeout.setValue(int(cfg.get("tape_end_timeout", 30)))
        if "purge_empty_clips" in cfg:
            self.chk_purge_empty.setChecked(cfg.get("purge_empty_clips", True))
        if "tape_timer_enabled" in cfg:
            self.chk_tape_timer.setChecked(cfg.get("tape_timer_enabled", False))
        if "tape_timer_mins" in cfg:
            self.spin_tape_mins.setValue(cfg.get("tape_timer_mins", 60))
            self._sync_tape_preset_btns(cfg.get("tape_timer_mins", 60))

        if "active_profile" in cfg and hasattr(self, "combo_profile"):
            act = cfg.get("active_profile", "")
            for i in range(self.combo_profile.count()):
                if self.combo_profile.itemData(i) == act:
                    self.combo_profile.blockSignals(True)
                    self.combo_profile.setCurrentIndex(i)
                    self.combo_profile.blockSignals(False)
                    break

    def _refresh_profile_list(self, select_name: str = ""):
        if not hasattr(self, "combo_profile"):
            return
        self.combo_profile.blockSignals(True)
        self.combo_profile.clear()
        all_profiles = list_profiles()
        for p in all_profiles:
            prefix = "⭐ " if p in _FACTORY_PRESETS else "👤 "
            self.combo_profile.addItem(f"{prefix}{p}", p)

        if select_name:
            for i in range(self.combo_profile.count()):
                if self.combo_profile.itemData(i) == select_name:
                    self.combo_profile.setCurrentIndex(i)
                    break
        self.combo_profile.blockSignals(False)

    def _on_profile_activated(self, index: int):
        profile_name = self.combo_profile.itemData(index)
        if not profile_name:
            return
        cfg = load_profile(profile_name)
        if cfg:
            self._restore_settings(cfg)
            self._log_event("info", f"Loaded profile: {profile_name}", "Capture settings applied")
            self._save_settings()

    def _save_profile_dialog(self):
        current_data = self._current_settings()
        curr_prof = self.combo_profile.currentData() if hasattr(self, "combo_profile") else "My Custom Profile"
        if curr_prof in _FACTORY_PRESETS or not curr_prof:
            curr_prof = "My Custom Profile"
        name, ok = QInputDialog.getText(
            self, "Save Profile", "Enter profile name:",
            QLineEdit.EchoMode.Normal, curr_prof
        )
        if ok and name.strip():
            clean_name = name.strip()
            if clean_name in _FACTORY_PRESETS:
                clean_name = clean_name + " (Custom)"
            save_profile(clean_name, current_data)
            self._refresh_profile_list(select_name=clean_name)
            self._save_settings()
            self._log_event("info", f"Saved profile '{clean_name}'", "Profile saved to presets list")

    def _manage_profiles_dialog(self):
        dlg = QDialog(self)
        dlg.setWindowTitle("Manage Capture Profiles")
        dlg.setFixedSize(440, 360)
        _icon_path = resource_path("vhs_capture.ico")
        if os.path.exists(_icon_path):
            dlg.setWindowIcon(QIcon(_icon_path))

        lay = QVBoxLayout(dlg)
        lay.setContentsMargins(18, 18, 18, 18)
        lay.setSpacing(10)

        lbl = QLabel("User Saved Profiles:")
        lbl.setStyleSheet(f"color:{TEXT};font-weight:600;font-size:13px")
        lay.addWidget(lbl)

        list_widget = QListWidget()
        list_widget.setStyleSheet(
            f"QListWidget{{background:{BG};border:1px solid {BORDER};border-radius:6px;"
            f"color:{TEXT};font-size:12px;padding:4px}}"
            f"QListWidget::item:selected{{background:#2a3a55;color:#fff}}"
        )

        user_profiles = []
        if _PROFILES_DIR.is_dir():
            user_profiles = sorted([p.stem for p in _PROFILES_DIR.glob("*.json")])

        if not user_profiles:
            list_widget.addItem("(No custom user profiles saved yet)")
            list_widget.setEnabled(False)
        else:
            for up in user_profiles:
                list_widget.addItem(up)
            list_widget.setCurrentRow(0)
        lay.addWidget(list_widget, 1)

        btn_box = QHBoxLayout()
        btn_del = QPushButton("Delete Selected")
        btn_del.setEnabled(bool(user_profiles))
        btn_del.setFixedHeight(30)
        btn_del.setStyleSheet(
            f"QPushButton{{border:1px solid {BORDER};border-radius:6px;padding:2px 12px;"
            f"background:{SURFACE};color:{RED};font-weight:600;font-size:12px}}"
            f"QPushButton:hover{{background:{RED_BG};border-color:{RED}}}"
            f"QPushButton:disabled{{color:#555}}"
        )

        def _do_del():
            item = list_widget.currentItem()
            if item and item.text() in user_profiles:
                del_name = item.text()
                delete_profile(del_name)
                row = list_widget.row(item)
                list_widget.takeItem(row)
                user_profiles.remove(del_name)
                self._refresh_profile_list()
                self._log_event("info", f"Deleted profile: {del_name}")
                if not user_profiles:
                    list_widget.addItem("(No custom user profiles saved yet)")
                    list_widget.setEnabled(False)
                    btn_del.setEnabled(False)

        btn_del.clicked.connect(_do_del)
        btn_box.addWidget(btn_del)

        btn_open = QPushButton("Open Folder")
        btn_open.setFixedHeight(30)
        btn_open.setStyleSheet(
            f"QPushButton{{border:1px solid {BORDER};border-radius:6px;padding:2px 12px;"
            f"background:{SURFACE};color:{TEXT};font-size:12px}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e}}"
        )
        def _do_open():
            _PROFILES_DIR.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(_PROFILES_DIR))
            else:
                subprocess.Popen(["xdg-open", str(_PROFILES_DIR)])

        btn_open.clicked.connect(_do_open)
        btn_box.addWidget(btn_open)

        btn_box.addStretch()
        btn_close = QPushButton("Close")
        btn_close.setFixedHeight(30)
        btn_close.setStyleSheet(
            f"QPushButton{{border:1px solid {BORDER};border-radius:6px;padding:2px 16px;"
            f"background:{SURFACE};color:{TEXT};font-size:12px;font-weight:500}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e}}"
        )
        btn_close.clicked.connect(dlg.accept)
        btn_box.addWidget(btn_close)

        lay.addLayout(btn_box)
        dlg.exec()

    def _save_settings(self):
        save_settings(self._current_settings())

    def _apply_windows_theme(self):
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
            return

        if is_light:
            app = QApplication.instance()
            p = app.palette()
            p.setColor(QPalette.ColorRole.Window,        QColor(245, 245, 245))
            p.setColor(QPalette.ColorRole.WindowText,    QColor(30, 30, 30))
            p.setColor(QPalette.ColorRole.Base,          QColor(255, 255, 255))
            p.setColor(QPalette.ColorRole.Text,          QColor(30, 30, 30))
            p.setColor(QPalette.ColorRole.Button,        QColor(235, 235, 235))
            p.setColor(QPalette.ColorRole.ButtonText,    QColor(30, 30, 30))
            app.setPalette(p)

    def _open_camera(self):
        dlg = CameraPickerDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return

        if self._capture:
            self._capture.stop()
            self._capture = None

        self._camera_index    = dlg.selected_index
        self._camera_native_w = dlg.selected_w
        self._camera_native_h = dlg.selected_h
        self._camera_native_fps = dlg.selected_fps

        if dlg.selected_h == 576:
            self.combo_res.setCurrentIndex(0)
        elif dlg.selected_h == 480:
            self.combo_res.setCurrentIndex(1)

        cfg = self._get_config()
        self._capture = CaptureThread(cfg)
        self._capture.frame_ready.connect(self._on_frame)
        self._capture.blue_changed.connect(self._on_blue_changed)
        self._capture.black_changed.connect(self._on_black_changed)
        self._capture.static_changed.connect(self._on_static_changed)
        self._capture.segment_split.connect(self._on_segment_split)
        self._capture.timecode_tick.connect(self._on_timecode)
        self._capture.sync_stats.connect(self._on_sync_stats)
        self._capture.tape_ended.connect(self._on_tape_ended)
        self._capture.error.connect(self._on_error)
        self._capture.start()

        if self._audio_meter:
            self._audio_meter.stop()
        if _AUDIO_AVAILABLE:
            cur_audio = "" if self.combo_audio.currentIndex() <= 0 else self.combo_audio.currentText()
            self._audio_meter = AudioMeterThread(cur_audio)
            self._audio_meter.level.connect(
                lambda v: self.audio_bar.setValue(int(v * 100))
            )
            self._audio_meter.level.connect(self._capture.set_audio_level)
            self._audio_meter.start()

        self.btn_rec.setEnabled(True)
        self.btn_settings.setEnabled(sys.platform == "win32")
        self.btn_snapshot.setEnabled(True)
        self.btn_cam.setText("📹  Change camera")
        self.badge_idle.setText("● Live")
        self._log_event("info", "Camera opened",
                        f"index {cfg['camera_index']} @ {cfg['width']}×{cfg['height']} {cfg['fps']}fps")

    def _open_device_settings(self):
        """Opens hardware capture card configuration dialog."""
        if self._capture:
            ok = self._capture.open_device_settings()
            if ok:
                self._log_event("info", "Hardware device dialog opened", "DirectShow properties")
            else:
                self._log_event("stop", "Device settings not supported", "Device driver did not expose a property sheet")

    def _take_snapshot(self):
        """Grabs full-resolution still from the capture card."""
        frame = self._last_preview_frame
        if frame is None:
            self._log_event("error", "Snapshot failed", "No video frame available")
            return

        out_dir = Path(self._output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        t = self.edit_title.text().strip()
        p = self.edit_part.text().strip()
        parts = [x for x in [t, p, "snapshot", ts] if x]
        snap_path = out_dir / f"{'_'.join(parts)}.png"

        try:
            cv2.imwrite(str(snap_path), frame)
            self._log_event("info", "📸 Snapshot saved", snap_path.name)
        except Exception as e:
            self._log_event("error", "Snapshot error", str(e))

    def _export_edl_and_chapters(self):
        """Exports CMX 3600 EDL and MKV chapter metadata for editing or media players."""
        if not self._clips:
            self._log_event("stop", "No clips to export", "Record or split clips first")
            return

        out_dir = Path(self._output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        t = self.edit_title.text().strip() or "VHS_Tape"
        p = self.edit_part.text().strip()
        ts = datetime.now().strftime("%Y%m%d")
        base = "_".join([x for x in [t, p, ts] if x])

        # 1. CMX 3600 EDL
        edl_file = out_dir / f"{base}.edl"
        edl_lines = [
            f"TITLE: {base}",
            "FCM: NON-DROP FRAME",
            "",
        ]
        curr_frame = 0
        fps = float(self.spin_fps.value())
        for i, (path_str, _) in enumerate(self._clips.items(), start=1):
            name = Path(path_str).name
            edl_lines.append(f"{i:03d}  AX       V     C        00:00:00:00 00:00:00:00 00:00:00:00 00:00:00:00")
            edl_lines.append(f"* FROM CLIP NAME: {name}")
            edl_lines.append("")

        edl_file.write_text("\n".join(edl_lines), encoding="utf-8")

        # 2. MKV / FFmpeg Chapters File
        ch_file = out_dir / f"{base}_chapters.txt"
        ch_lines = [";FFMETADATA1"]
        curr_ms = 0
        for i, (fname, dur_sec) in enumerate(self._segment_records, start=1):
            end_ms = curr_ms + int(dur_sec * 1000)
            ch_lines.append("[CHAPTER]")
            ch_lines.append("TIMEBASE=1/1000")
            ch_lines.append(f"START={curr_ms}")
            ch_lines.append(f"END={end_ms}")
            ch_lines.append(f"title=Part {i} - {fname}")
            ch_lines.append("")
            curr_ms = end_ms

        ch_file.write_text("\n".join(ch_lines), encoding="utf-8")
        self._log_event("info", "EDL & Chapters exported", f"{edl_file.name} / {ch_file.name}")

    def _toggle_record(self):
        if not self._recording:
            self._start_recording()
        else:
            self._stop_recording()

    def _start_recording(self):
        if not self._capture:
            return
        self._capture.cfg.update(self._get_config())
        self._capture.start_recording()
        self._recording = True
        self._seg_count = 0
        self._segment_records.clear()
        self.stat_clips.setText(str(len(self._clips)))
        self.stat_splits.setText("0")
        self.stat_sync.setText("100% sync")
        self.btn_rec.setText("⏹  Stop recording")
        self.btn_rec.setStyleSheet(
            f"QPushButton{{background:{RED_BG};color:{RED};border:1px solid {RED};"
            f"border-radius:8px;padding:0 18px;font-size:13px;font-weight:500;height:36px}}"
            f"QPushButton:hover{{background:#3d1012;border-color:#ff4d4d;color:#fff}}"
            f"QPushButton:pressed{{background:#280808}}"
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
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
            f"QPushButton:pressed{{background:#1d2028}}"
            f"QPushButton:disabled{{color:#444;border-color:{BORDER}}}"
        )
        self.btn_split.setEnabled(False)
        self.badge_rec.setVisible(False)
        self.badge_blue.setVisible(False)
        self.badge_black.setVisible(False)
        self.badge_static.setVisible(False)
        self.badge_idle.setVisible(True)
        self.badge_idle.setText("● Idle")
        self.blue_bar.setValue(0)
        self.lbl_tape_remaining.setText("—")
        self._log_event("stop", "Recording stopped",
                        f"{self._seg_count} auto-splits · {len(self._clips)} clips")

        if self.chk_purge_empty.isChecked():
            # Run scan in background once ffmpeg muxers finish flushing
            QTimer.singleShot(1200, lambda: self._purge_empty_clips(silent=False))

    def _purge_empty_clips_manual(self):
        """Manual purge trigger from Saved Clips toolbar."""
        self._purge_empty_clips(silent=False)

    def _purge_empty_clips(self, silent: bool = False):
        """Scans current recorded clips and purges any that contain zero content or blank frames."""
        clips_snapshot = list(self._clips.keys())
        if not clips_snapshot:
            if not silent:
                self._log_event("info", "Purge empty clips", "No clips in list to evaluate")
            return

        def _worker():
            purged_any = 0
            for path_str in clips_snapshot:
                # Wait briefly if active muxing is finalizing
                for _ in range(6):
                    if not Path(path_str + ".mux_tmp" + Path(path_str).suffix).exists():
                        break
                    time.sleep(0.3)

                is_empty, reason = probe_clip_is_empty(path_str)
                if is_empty:
                    p = Path(path_str)
                    try:
                        p.unlink(missing_ok=True)
                    except Exception as e:
                        pass
                    purged_any += 1
                    self._clip_purged.emit(path_str, reason)

            if not silent:
                if purged_any == 0:
                    self._log_event("info", "Purge check finished", "All saved clips contain valid content")
                else:
                    self._log_event("split", f"Purge complete: {purged_any} empty clip(s) removed",
                                    f"{len(self._clips)} valid clip(s) remaining")

        threading.Thread(target=_worker, daemon=True, name="purge-empty-clips").start()

    def _on_clip_purged(self, real_path: str, reason: str):
        """UI slot called when an empty clip was detected and deleted from disk."""
        name = Path(real_path).name
        item = self._clips.pop(real_path, None)
        if item:
            row = self.clips_list.row(item)
            if row >= 0:
                self.clips_list.takeItem(row)
            del item

        # Remove from segment records for EDL/chapters
        self._segment_records = [r for r in self._segment_records if r[0] != name]
        self.stat_clips.setText(str(len(self._clips)))

        # Renumber remaining items in UI list
        for idx in range(self.clips_list.count()):
            it = self.clips_list.item(idx)
            if it:
                it_path = it.data(Qt.ItemDataRole.UserRole)
                it_name = Path(it_path).name
                txt = it.text().strip()
                # Preserve existing size text if present
                if "(" in txt and txt.endswith(")"):
                    sz_part = txt[txt.find("("):]
                    it.setText(f"  {idx+1:02d}  {it_name}  {sz_part}")
                else:
                    it.setText(f"  {idx+1:02d}  {it_name}")

        self._log_event("info", f"🗑️ Purged empty clip: {name}", reason)

    def _manual_split(self):
        if self._capture and self._recording:
            self._capture.manual_split()
            self._log_event("split", "Manual split", f"at {self.lbl_timecode.text()}")

    def _on_frame(self, frame: np.ndarray):
        self._last_preview_frame = frame
        h, w = frame.shape[:2]
        img = QImage(frame.data, w, h, frame.strides[0], QImage.Format.Format_BGR888)
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
        if is_blue:
            self._seg_count += 1
            self.stat_splits.setText(str(self._seg_count))
            self._log_event("blue", "Blue screen detected", f"{pct}% blue")
        else:
            self._log_event("resume", "Video resumed", "new segment started")

    def _on_black_changed(self, is_black: bool, ratio: float):
        self._in_black = is_black
        self.badge_black.setVisible(is_black)
        if is_black:
            pct = int(ratio * 100)
            self._log_event("info", "Blank screen detected", f"{pct}% black pixels")

    def _on_static_changed(self, is_static: bool):
        self.badge_static.setVisible(is_static)

    def _on_tape_ended(self, reason: str):
        self._log_event("stop", "Tape End Detected", f"{reason} — auto-stopping recording")
        self._stop_recording()

    def _on_segment_split(self, path: str, elapsed: float):
        if path.endswith(":muxed"):
            real = path[:-6]
            self._log_event("info", "Clip finalized", Path(real).name)
            item = self._clips.get(real)
            if item:
                def _refresh():
                    try:
                        sz = Path(real).stat().st_size
                        if sz > 0:
                            self._clip_size_updated.emit(item, Path(real).name, sz / 1024 / 1024)
                    except Exception:
                        pass
                threading.Thread(target=_refresh, daemon=True).start()
            return

        clip_num = len(self._clips) + 1
        name = Path(path).name
        self._segment_records.append((name, elapsed))
        self._log_event("split", f"New segment: {name}", f"t={elapsed:.1f}s")
        item = QListWidgetItem(f"  {clip_num:02d}  {name}   recording…")
        item.setData(Qt.ItemDataRole.UserRole, path)
        item.setToolTip(path)
        self.clips_list.addItem(item)
        self._clips[path] = item
        self.stat_clips.setText(str(len(self._clips)))
        self.clips_list.scrollToBottom()

    def _on_sync_stats(self, written: int, duplicated: int):
        if self._recording:
            if duplicated == 0:
                self.stat_sync.setText("100% sync")
            else:
                self.stat_sync.setText(f"{duplicated} filled")

    def _open_clip_folder(self, item: QListWidgetItem):
        path = item.data(Qt.ItemDataRole.UserRole)
        folder = str(Path(path).parent)
        if sys.platform == "win32":
            subprocess.Popen(["explorer", "/select,", path.replace("/", "\\")], **_NO_WINDOW)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _update_clip_item(self, item: QListWidgetItem, name: str, size_mb: float):
        current_text = item.text().strip()
        clip_num = current_text.split("  ", 1)[0] if "  " in current_text else "01"
        item.setText(f"  {clip_num}  {name}  ({size_mb:.1f} MB)")
        item.setForeground(QColor(MUTED))

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

    def _pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Select output directory", self._output_dir)
        if d:
            self._output_dir = d
            self.lbl_outdir.setText(d)
            self._check_disk_space()

    def _log_event(self, kind: str, msg: str, detail: str = ""):
        self._log.add(kind, msg, detail)
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

    def _clear_log(self):
        self._log.clear()
        self.log_view.clear()

    def _show_about_dialog(self):
        """Displays system, framework, and component versions information dialog."""
        dlg = QDialog(self)
        dlg.setWindowTitle("About VHS Capture")
        dlg.setFixedSize(540, 480)
        _icon_path = resource_path("vhs_capture.ico")
        if os.path.exists(_icon_path):
            dlg.setWindowIcon(QIcon(_icon_path))

        dlg_layout = QVBoxLayout(dlg)
        dlg_layout.setContentsMargins(20, 20, 20, 20)
        dlg_layout.setSpacing(14)

        # Header with icon and app title
        top_row = QHBoxLayout()
        if os.path.exists(_icon_path):
            icon_lbl = QLabel()
            icon_lbl.setPixmap(QIcon(_icon_path).pixmap(48, 48))
            top_row.addWidget(icon_lbl)
            top_row.addSpacing(12)

        vbox_title = QVBoxLayout()
        t_lbl = QLabel(f"VHS Capture v{__version__}")
        t_lbl.setStyleSheet(f"color:{TEXT};font-size:18px;font-weight:700")
        s_lbl = QLabel("Professional VHS Tape Digitizer & Auto-Splitter")
        s_lbl.setStyleSheet(f"color:{MUTED};font-size:12px")
        vbox_title.addWidget(t_lbl)
        vbox_title.addWidget(s_lbl)
        top_row.addLayout(vbox_title)
        top_row.addStretch()
        dlg_layout.addLayout(top_row)

        # Details group box
        details_box = QGroupBox("System & Framework Versions")
        details_box.setStyleSheet(
            f"QGroupBox{{color:{TEXT};font-weight:600;border:1px solid {BORDER};"
            f"border-radius:8px;margin-top:10px;padding-top:12px}}"
            f"QGroupBox::title{{subcontrol-origin:margin;left:10px;padding:0 5px}}"
        )
        grid = QGridLayout(details_box)
        grid.setContentsMargins(14, 14, 14, 14)
        grid.setHorizontalSpacing(16)
        grid.setVerticalSpacing(8)

        # Probe components
        py_ver = f"{sys.version.split()[0]} ({platform.architecture()[0]})"
        qt_ver = f"PyQt6 {PYQT_VERSION_STR} (Qt {qVersion()})"
        cv_ver = getattr(cv2, "__version__", "Unknown")

        # Rust core
        if _RUST_AVAILABLE:
            rust_status = "v0.4.0 (Active · SIMD acceleration enabled)"
        else:
            rust_status = "Fallback mode (Pure Python/NumPy)"

        # Audio
        if _AUDIO_AVAILABLE:
            audio_ver = f"v{getattr(sd, '__version__', 'Installed')}"
        else:
            audio_ver = "Unavailable"

        # FFmpeg
        ff_path = _system_ffmpeg() or FFMPEG_BIN
        ff_ver_str = "Bundled (imageio-ffmpeg)"
        if ff_path:
            try:
                res = subprocess.run([ff_path, "-version"], capture_output=True, text=True, timeout=2, **_NO_WINDOW)
                first_line = res.stdout.splitlines()[0] if res.stdout else ""
                if "ffmpeg version" in first_line:
                    ff_ver_str = first_line.split("Copyright")[0].strip()
            except Exception:
                pass

        os_str = f"{platform.system()} {platform.release()} (Build {platform.version()})"

        specs = [
            ("Application Version:", f"v{__version__}"),
            ("Rust Core Engine:", rust_status),
            ("FFmpeg Encoder:", ff_ver_str),
            ("Python Runtime:", py_ver),
            ("GUI Framework:", qt_ver),
            ("OpenCV Library:", cv_ver),
            ("Audio Subsystem:", f"sounddevice {audio_ver}"),
            ("Operating System:", os_str),
        ]

        for row, (label, val) in enumerate(specs):
            l = QLabel(label)
            l.setStyleSheet(f"color:{MUTED};font-weight:500;font-size:12px")
            v = QLabel(val)
            v.setStyleSheet(f"color:{TEXT};font-weight:600;font-size:12px")
            v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            grid.addWidget(l, row, 0)
            grid.addWidget(v, row, 1)

        dlg_layout.addWidget(details_box)

        # Action buttons
        btn_row = QHBoxLayout()
        btn_copy = QPushButton("Copy Specs")
        btn_copy.setFixedHeight(32)
        btn_copy.setStyleSheet(
            f"QPushButton{{border:1px solid {BORDER};border-radius:6px;padding:0 12px;"
            f"background:{SURFACE};color:{TEXT};font-size:12px;font-weight:500}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
        )

        def _copy_specs():
            text_lines = [f"{k} {v}" for k, v in specs]
            clip_text = "VHS Capture System Information:\n" + "\n".join(text_lines)
            QApplication.clipboard().setText(clip_text)
            btn_copy.setText("✓ Copied!")
            QTimer.singleShot(2000, lambda: btn_copy.setText("Copy Specs"))

        btn_copy.clicked.connect(_copy_specs)
        btn_row.addWidget(btn_copy)
        btn_row.addStretch()

        btn_close = QPushButton("Close")
        btn_close.setFixedHeight(32)
        btn_close.setStyleSheet(
            f"QPushButton{{border:1px solid {BORDER};border-radius:6px;padding:0 16px;"
            f"background:{SURFACE};color:{TEXT};font-size:12px;font-weight:500}}"
            f"QPushButton:hover{{background:#262933;border-color:#454c5e;color:#fff}}"
        )
        btn_close.clicked.connect(dlg.accept)
        btn_row.addWidget(btn_close)

        dlg_layout.addLayout(btn_row)
        dlg.exec()

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
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("vhs.capture.app")
        except Exception:
            pass

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    _icon_path = resource_path("vhs_capture.ico")
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
