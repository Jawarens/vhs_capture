"""
Unit tests for VHS signal analysis: Blue screen, Black screen, Luma, and Analog Static / Snow.
"""

import numpy as np
from vhs_lib.signals import (
    _numpy_blue_ratio,
    analyse_blue_ratio,
    analyse_signals,
)


def test_blue_screen_detection():
    # Pure VCR Blue Screen (BGR = 210, 30, 20)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[:, :] = (210, 30, 20)

    sig = analyse_signals(frame)
    assert sig["blue_ratio"] > 0.95
    assert sig["black_ratio"] == 0.0
    assert not sig["is_static"]
    assert sig["avg_luma"] > 35.0

    # Test backwards-compatible wrapper
    assert analyse_blue_ratio(frame) > 0.95


def test_black_screen_detection():
    # Pure blank unrecorded tape / black screen (BGR = 0, 0, 0)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)

    sig = analyse_signals(frame)
    assert sig["blue_ratio"] == 0.0
    assert sig["black_ratio"] == 1.0
    assert sig["avg_luma"] < 1.0
    assert not sig["is_static"]


def test_analog_static_detection():
    # Simulated analog RF snow / static: high contrast noise across gray spectrum
    np.random.seed(42)
    # Binary/extreme random noise produces high std (>45) and mid luma
    noise = (np.random.randint(0, 2, size=(480, 640), dtype=np.uint8) * 230) + 10
    frame = np.stack([noise, noise, noise], axis=-1)

    sig = analyse_signals(frame)
    assert sig["is_static"] is True
    assert sig["blue_ratio"] < 0.1


def test_natural_content_no_false_alarm():
    # Typical video frame: warm tones / skin tone & greenery (non-blue, non-static)
    h, w = 480, 640
    frame = np.zeros((h, w, 3), dtype=np.uint8)
    frame[:, :, 0] = 70   # Blue
    frame[:, :, 1] = 130  # Green
    frame[:, :, 2] = 180  # Red (warm)

    sig = analyse_signals(frame)
    assert sig["blue_ratio"] < 0.05
    assert sig["black_ratio"] < 0.05
    assert sig["is_static"] is False


def test_numpy_fallback_parity():
    # Verify NumPy blue ratio calculation directly
    frame = np.zeros((45, 80, 3), dtype=np.uint8)
    frame[:, :] = (210, 30, 20)
    assert _numpy_blue_ratio(frame, 80, 45) > 0.95
