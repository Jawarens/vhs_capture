"""
Audio device enumeration, sounddevice VU metering, and raw PCM recording/muxing.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PyQt6.QtCore import QThread, pyqtSignal

if TYPE_CHECKING:
    from .ffmpeg import FFmpegWriter

logger = logging.getLogger(__name__)

# On Windows, suppress console window flash
_NO_WINDOW: dict[str, int] = (
    {"creationflags": subprocess.CREATE_NO_WINDOW}
    if sys.platform == "win32"
    else {}
)

try:
    import sounddevice as sd
    _AUDIO_AVAILABLE = True
except (ImportError, OSError):
    sd = None
    _AUDIO_AVAILABLE = False


class AudioMeterThread(QThread):
    """Reads microphone/line-in and emits RMS level (0.0–1.0) 20x/sec."""
    level = pyqtSignal(float)

    def __init__(self, device_name: str = ""):
        super().__init__()
        self.device_name = device_name
        self._running = False

    def run(self):
        if not _AUDIO_AVAILABLE or sd is None:
            return
        self._running = True
        try:
            device_idx = None
            if self.device_name and self.device_name.lower() != "no audio":
                try:
                    for i, d in enumerate(sd.query_devices()):
                        if (
                            d.get("max_input_channels", 0) > 0
                            and self.device_name.lower() in d["name"].lower()
                        ):
                            device_idx = i
                            break
                except (OSError, RuntimeError, ValueError) as e:
                    logger.debug("Failed querying sounddevice input devices: %s", e)

            with sd.InputStream(device=device_idx, channels=1, dtype="float32") as stream:
                actual_rate = stream.samplerate or 44100
                block = max(512, int(actual_rate // 20))  # ~50ms blocks
                while self._running:
                    data, _ = stream.read(block)
                    rms = float(np.sqrt(np.mean(data ** 2)))
                    db = max(-60.0, 20 * np.log10(rms + 1e-9))
                    lvl = (db + 60) / 60
                    self.level.emit(min(1.0, max(0.0, lvl)))
        except (OSError, RuntimeError) as e:
            logger.debug("AudioMeterThread encountered error: %s", e)

    def stop(self):
        self._running = False
        self.quit()
        self.wait(1000)


def list_audio_devices(ffmpeg_bin: str) -> list[str]:
    """Enumerate audio input devices available to FFmpeg."""
    devices: list[str] = []

    if sys.platform == "win32":
        cmd = [
            ffmpeg_bin, "-list_devices", "true",
            "-f", "dshow", "-i", "dummy",
            "-loglevel", "debug",
        ]
        try:
            r = subprocess.run(
                cmd, capture_output=True, timeout=6, check=False, **_NO_WINDOW
            )
            output = r.stderr.decode(errors="replace")
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
        except (subprocess.TimeoutExpired, OSError) as e:
            logger.debug("DirectShow audio device query error: %s", e)
    else:
        try:
            r = subprocess.run(
                ["pactl", "list", "sources", "short"],
                capture_output=True, timeout=5, check=False, **_NO_WINDOW
            )
            for line in r.stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1] not in devices:
                    devices.append(parts[1])
        except (subprocess.TimeoutExpired, OSError):
            pass

    if not devices and _AUDIO_AVAILABLE and sd is not None:
        try:
            for d in sd.query_devices():
                if d.get("max_input_channels", 0) > 0 and d["name"] not in devices:
                    devices.append(d["name"])
        except (OSError, RuntimeError) as e:
            logger.debug("Fallback audio enumeration error: %s", e)

    return devices


class AudioRecorder:
    """
    Captures audio into raw PCM and muxes with precision A/V async resample,
    mono duplication, 50/60Hz mains hum suppression, and normalization.
    """

    def __init__(
        self,
        device_name: str,
        temp_path: str,
        ffmpeg_bin: str,
        channel_mode: str = "stereo",
        hum_filter: bool = False,
        normalize: bool = False,
        is_lossless: bool = False,
    ):
        self.device_name = device_name
        self.temp_path = temp_path
        self.ffmpeg_bin = ffmpeg_bin
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
            **_NO_WINDOW,
        )

        def _drain_stderr():
            if self._proc and self._proc.stderr:
                for _line in self._proc.stderr:
                    pass

        threading.Thread(target=_drain_stderr, daemon=True, name="audio-stderr").start()

    def stop_and_mux(
        self,
        video_path: str,
        video_writer: FFmpegWriter | None = None,
        on_done: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ):
        def _work():
            if video_writer:
                video_writer.stop()

            if self._proc:
                try:
                    self._proc.terminate()
                except OSError:
                    pass
                try:
                    self._proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    try:
                        self._proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
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
            is_mp4 = vpath.suffix.lower() == ".mp4"
            is_mkv = vpath.suffix.lower() == ".mkv"

            # Use lossless FLAC for MKV/FFV1 or AAC 192k for MP4/delivery
            audio_codec = (
                ["-c:a", "flac"]
                if (is_mkv and self.is_lossless)
                else ["-c:a", "aac", "-b:a", "192k"]
            )

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
                r = subprocess.run(
                    cmd, capture_output=True, timeout=180, check=False, **_NO_WINDOW
                )
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
            except (OSError, subprocess.SubprocessError) as e:
                if on_error:
                    on_error(str(e))
                out_tmp.unlink(missing_ok=True)
                if on_done:
                    on_done()
            finally:
                pcm.unlink(missing_ok=True)

        threading.Thread(target=_work, daemon=True, name="audio-mux").start()


class _AudioScanner(QThread):
    done = pyqtSignal(list)

    def run(self):
        from .ffmpeg import FFMPEG_BIN, _system_ffmpeg
        ff = _system_ffmpeg() or FFMPEG_BIN
        devs = list_audio_devices(ff)
        self.done.emit(devs)
