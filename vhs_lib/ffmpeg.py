"""
FFmpeg discovery, hardware encoder profiling, process management, and writer thread.
"""

from __future__ import annotations

import logging
import os
import queue as _queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

# On Windows, suppress console window flash for subprocesses
_NO_WINDOW: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)

# Check if native Rust frame queue is available
try:
    import vhs_core as _vhs_core
    _RUST_AVAILABLE = hasattr(_vhs_core, "FrameQueue")
except ImportError:
    _vhs_core = None
    _RUST_AVAILABLE = False


def resource_path(relative_path: str) -> str:
    """Get absolute path to resource, works for dev and for PyInstaller."""
    base_path = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


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
            os.path.join(
                local_app,
                "Microsoft",
                "WinGet",
                "Packages",
                "Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe",
                "ffmpeg-7.1-full_build",
                "bin",
                "ffmpeg.exe",
            ),
            os.path.join("C:\\", "ProgramData", "chocolatey", "bin", "ffmpeg.exe"),
            os.path.join("C:\\", "tools", "ffmpeg", "bin", "ffmpeg.exe"),
            os.path.join(user_profile, "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
            os.path.join(prog_data, "scoop", "apps", "ffmpeg", "current", "bin", "ffmpeg.exe"),
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
    # Fallback to imageio-ffmpeg bundled binary
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except (ImportError, OSError):
        pass
    return "ffmpeg"


FFMPEG_BIN = _find_ffmpeg()

# Full encoder profiles including Lossless Archival Master (FFV1)
_ENCODER_PROFILES = [
    # ── NVIDIA NVENC ─────────────────────────────────────────────────
    {
        "name": "av1_nvenc",
        "label": "AV1  —  NVIDIA NVENC ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    {
        "name": "hevc_nvenc",
        "label": "HEVC  —  NVIDIA NVENC ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    {
        "name": "h264_nvenc",
        "label": "H.264  —  NVIDIA NVENC ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-gpu", "0", "-preset", "p5", "-tune", "hq",
            "-rc", "constqp", "-qp", str(q), "-surfaces", "16",
            "-spatial-aq", "1", "-temporal-aq", "1",
        ],
    },
    # ── Intel QuickSync ──────────────────────────────────────────────
    {
        "name": "av1_qsv",
        "label": "AV1  —  Intel QuickSync ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    {
        "name": "hevc_qsv",
        "label": "HEVC  —  Intel QuickSync ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    {
        "name": "h264_qsv",
        "label": "H.264  —  Intel QuickSync ⚡",
        "is_hw": True,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": True,
        "probe_size": "256x256",
        "args": lambda q, w, h: [
            "-preset", "4", "-global_quality", str(q), "-look_ahead_depth", "0",
        ],
    },
    # ── Software / CPU ───────────────────────────────────────────────
    {
        "name": "libsvtav1",
        "label": "AV1  —  SVT-AV1 (CPU fast)",
        "is_hw": False,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "probe_size": "64x64",
        "args": lambda q, w, h: ["-preset", "6", "-crf", str(q), "-threads", "0"],
    },
    {
        "name": "libaom-av1",
        "label": "AV1  —  libaom (CPU best quality)",
        "is_hw": False,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-cpu-used", "6", "-b:v", "0", "-crf", str(q),
            "-threads", "0", "-row-mt", "1",
        ],
    },
    {
        "name": "libx265",
        "label": "HEVC / H.265  —  CPU",
        "is_hw": False,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-preset", "fast", "-crf", str(q),
            "-threads", "0", "-x265-params", "pools=+:frame-threads=4",
        ],
    },
    {
        "name": "libx264",
        "label": "H.264  —  CPU",
        "is_hw": False,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "probe_size": "64x64",
        "args": lambda q, w, h: ["-preset", "fast", "-crf", str(q), "-threads", "0"],
    },
    # ── Archival Lossless ────────────────────────────────────────────
    {
        "name": "ffv1",
        "label": "FFV1  —  Lossless Archival Master (MKV)",
        "is_hw": False,
        "pixfmt": "yuv420p",
        "requires_hw_ffmpeg": False,
        "probe_size": "64x64",
        "args": lambda q, w, h: [
            "-level", "3", "-g", "1", "-slices", "16", "-slicecrc", "1", "-threads", "0",
        ],
    },
]


def _friendly_hw_error(enc: str, stderr: str) -> str:
    s = stderr.lower()
    is_qsv = "qsv" in enc
    is_nvenc = "nvenc" in enc

    if is_qsv:
        if "invalid argument" in s or "error code: -22" in s:
            return (
                f"{enc}: Intel MFX/OneVPL runtime not installed. "
                "Install Intel Graphics Driver and oneVPL GPU Runtime"
            )
        if "mfx session" in s or "mfxinit" in s or "error creating" in s:
            return f"{enc}: MFX session failed — install Intel oneVPL GPU Runtime"
        if "unsupported" in s or "not support" in s:
            return f"{enc}: Intel GPU too old for this codec (requires 6th gen+)"
        if "no capable devices" in s or "device open failed" in s:
            return f"{enc}: no Intel GPU found or driver not loaded"

    if is_nvenc:
        if "invalid argument" in s or "error code: -22" in s:
            return (
                f"{enc}: invalid argument — driver mismatch or codec not supported "
                "(AV1 NVENC requires RTX 4000+)"
            )
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
    sys_ff = _system_ffmpeg()
    bnd_ff = FFMPEG_BIN

    def _test(profile):
        enc = profile["name"]
        pixfmt = profile["pixfmt"]
        psize = profile.get("probe_size", "256x256")
        pw, ph = (int(x) for x in psize.split("x"))
        args = profile["args"](28, pw, ph)
        needs_sys = profile.get("requires_hw_ffmpeg", False)
        ff = sys_ff if needs_sys else bnd_ff

        if ff is None:
            if profile["is_hw"]:
                return "failed", {
                    **profile,
                    "fail_reason": f"{enc}: system FFmpeg not found — run: winget install ffmpeg",
                }
            return "skip", None

        black = bytes(pw * ph * 3)
        cmd = [
            ff, "-y", "-loglevel", "error",
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-s", psize, "-r", "25", "-i", "pipe:0",
            "-c:v", enc,
        ] + args + ["-pix_fmt", pixfmt, "-frames:v", "2", "-f", "null", "-"]

        try:
            r = subprocess.run(
                cmd, input=black, capture_output=True, timeout=15, check=False, **_NO_WINDOW
            )
            if r.returncode == 0:
                p = dict(profile)
                p["ffmpeg_bin"] = ff
                return "ok", p
            elif profile["is_hw"]:
                stderr = r.stderr.decode(errors="replace")
                return "failed", {
                    **profile,
                    "fail_reason": _friendly_hw_error(enc, stderr),
                    "stderr_raw": stderr[-500:],
                }
            return "skip", None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            if profile["is_hw"]:
                return "failed", {**profile, "fail_reason": f"{enc}: probe timed out"}
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


class FFmpegWriter:
    """Wraps an ffmpeg process that accepts raw BGR frames on stdin."""

    def __init__(
        self,
        output_path: str,
        width: int,
        height: int,
        fps: float,
        encoder: str = "libsvtav1",
        crf: int = 28,
        encoder_info: dict | None = None,
        aspect_ratio: str = "4:3",
        deinterlace: str = "off",
        mask_head_switching: bool = True,
    ):
        self.output_path = output_path
        self.width = width
        self.height = height
        self.fps = fps
        self.encoder = encoder
        self.crf = crf
        self.encoder_info = encoder_info
        self.aspect_ratio = aspect_ratio
        self.deinterlace = deinterlace
        self.mask_head_switching = mask_head_switching
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._stderr_lines: list[str] = []

    def start(self):
        info = self.encoder_info or {}
        enc = self.encoder
        pixfmt = info.get("pixfmt", "yuv420p")
        ff = info.get("ffmpeg_bin", FFMPEG_BIN)

        args_fn = info.get("args")
        enc_args = (
            args_fn(self.crf, self.width, self.height)
            if args_fn
            else ["-crf", str(self.crf), "-threads", "0"]
        )

        # Build video filter chain (deinterlace + head switching mask)
        vf_filters = []
        if self.mask_head_switching:
            vf_filters.append("drawbox=y=ih-12:h=12:color=black:t=fill")

        is_nvenc = "nvenc" in enc
        has_cuda_filters = False
        if is_nvenc and self.deinterlace in ("bwdif", "bwdif_2x", "yadif"):
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
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            **_NO_WINDOW,
        )
        self._stderr_lines = []
        threading.Thread(target=self._drain_stderr, daemon=True, name="ffmpeg-stderr").start()

    def _drain_stderr(self):
        if self._proc and self._proc.stderr:
            for line in self._proc.stderr:
                decoded = line.decode(errors="replace").strip()
                if not decoded:
                    continue
                if decoded.startswith(("frame=", "size=", "video:", "audio:")):
                    continue
                self._stderr_lines.append(decoded)
                if len(self._stderr_lines) > 200:
                    self._stderr_lines = self._stderr_lines[-100:]

    def last_errors(self, n: int = 5) -> list[str]:
        return self._stderr_lines[-n:]

    def write_raw(self, raw: bytes) -> bool:
        """
        Write raw frame bytes to FFmpeg stdin.
        Returns True on successful pipe write, or False if FFmpeg crashed / broke pipe.
        """
        with self._lock:
            if not self._proc or not self._proc.stdin:
                return False
            if self._proc.poll() is not None:
                return False
            try:
                self._proc.stdin.write(raw)
                return True
            except (BrokenPipeError, OSError):
                return False

    def stop(self):
        with self._lock:
            if self._proc:
                try:
                    if self._proc.stdin:
                        self._proc.stdin.close()
                except (OSError, BrokenPipeError):
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
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class _WriterThread(threading.Thread):
    """Dedicated thread that drains frame queue into active FFmpegWriter."""

    def __init__(self, capacity: int = 12, on_error: Callable[[list[str]], None] | None = None):
        super().__init__(daemon=True, name="writer-thread")
        if _RUST_AVAILABLE and _vhs_core is not None:
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
        self._error_emitted = False

    def set_writer(self, w: FFmpegWriter | None) -> FFmpegWriter | None:
        with self._lock:
            old = self._writer
            if old:
                self._flush_queue_to(old)
            self._writer = w
            self._error_emitted = False
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
                ok = w.write_raw(raw)
                if not ok and self._on_error and not self._error_emitted:
                    self._error_emitted = True
                    errs = w.last_errors(5)
                    self._on_error(
                        errs if errs else ["FFmpeg process exited or closed standard input unexpectedly."]
                    )

    def stop(self):
        self._running = False
        self._has_item.set()
        self.join(timeout=5)
        with self._lock:
            if self._writer:
                self._writer.stop()
                self._writer = None
