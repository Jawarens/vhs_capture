"""
Settings, user profiles, and factory hardware presets.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_SETTINGS_PATH = Path.home() / ".vhs_capture_settings.json"
_PROFILES_DIR = Path.home() / ".vhs_capture_profiles"

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


def load_settings(path: Path | None = None) -> dict:
    target = path or _SETTINGS_PATH
    try:
        if target.is_file():
            return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("Could not read settings from %s: %s", target, e)
    return {}


def save_settings(d: dict, path: Path | None = None):
    target = path or _SETTINGS_PATH
    try:
        target.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not save settings to %s: %s", target, e)


def list_profiles(profiles_dir: Path | None = None) -> list[str]:
    """Return all available preset names followed by saved user profile names."""
    names = list(_FACTORY_PRESETS.keys())
    pdir = profiles_dir or _PROFILES_DIR
    if pdir.is_dir():
        for p in sorted(pdir.glob("*.json")):
            stem = p.stem
            if stem not in names:
                names.append(stem)
    return names


def load_profile(name: str, profiles_dir: Path | None = None) -> dict:
    if name in _FACTORY_PRESETS:
        return dict(_FACTORY_PRESETS[name])
    pdir = profiles_dir or _PROFILES_DIR
    p = pdir / f"{name}.json"
    if p.is_file():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Could not load profile %s: %s", name, e)
    return {}


def save_profile(name: str, data: dict, profiles_dir: Path | None = None):
    pdir = profiles_dir or _PROFILES_DIR
    pdir.mkdir(parents=True, exist_ok=True)
    p = pdir / f"{name}.json"
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")


def delete_profile(name: str, profiles_dir: Path | None = None) -> bool:
    pdir = profiles_dir or _PROFILES_DIR
    p = pdir / f"{name}.json"
    if p.is_file():
        try:
            p.unlink()
            return True
        except OSError as e:
            logger.warning("Could not delete profile %s: %s", name, e)
    return False

