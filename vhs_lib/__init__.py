"""
vhs_lib — Core library modules for VHS Capture.
"""

__version__ = "0.4.1"

from .audio import (
    AudioMeterThread,
    AudioRecorder,
    list_audio_devices,
)
from .config import (
    _FACTORY_PRESETS,
    delete_profile,
    list_profiles,
    load_profile,
    load_settings,
    save_profile,
    save_settings,
)
from .devices import (
    _enumerate_cameras,
    _probe_camera,
)
from .edl import (
    frames_to_tc,
    generate_chapters,
    generate_edl,
    tc_to_frames,
)
from .ffmpeg import (
    FFmpegWriter,
    _find_ffmpeg,
    _friendly_hw_error,
    _system_ffmpeg,
    _WriterThread,
    probe_encoders,
)
from .signals import (
    analyse_blue_ratio,
    analyse_signals,
)

__all__ = [
    "_FACTORY_PRESETS",
    "AudioMeterThread",
    "AudioRecorder",
    "FFmpegWriter",
    "_WriterThread",
    "__version__",
    "_enumerate_cameras",
    "_find_ffmpeg",
    "_friendly_hw_error",
    "_probe_camera",
    "_system_ffmpeg",
    "analyse_blue_ratio",
    "analyse_signals",
    "delete_profile",
    "frames_to_tc",
    "generate_chapters",
    "generate_edl",
    "list_audio_devices",
    "list_profiles",
    "load_profile",
    "load_settings",
    "probe_encoders",
    "save_profile",
    "save_settings",
    "tc_to_frames",
]
