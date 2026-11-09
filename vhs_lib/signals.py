"""
Signal analysis for VHS capture: Blue screen, Black screen, Luma, and Analog Static / Snow.
Provides a native Rust SIMD hot-path with a full NumPy fallback.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Try to load the Rust extension
try:
    import vhs_core as _vhs_core
    _RUST_AVAILABLE = True
    _RUST_HAS_ANALYSE_SIGNALS = hasattr(_vhs_core, "analyse_signals")
except ImportError:
    _vhs_core = None
    _RUST_AVAILABLE = False
    _RUST_HAS_ANALYSE_SIGNALS = False


def _numpy_blue_ratio(small: np.ndarray, sample_w: int, sample_h: int) -> float:
    """NumPy implementation of VHS blue detection."""
    b_ch = small[:, :, 0].astype(np.float32)
    g_ch = small[:, :, 1].astype(np.float32)
    r_ch = small[:, :, 2].astype(np.float32)
    maxc = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    minc = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    d = maxc - minc + 1e-6
    sat = np.divide(d, maxc, out=np.zeros_like(maxc), where=maxc > 0)
    hue = np.zeros_like(maxc)
    mask_r = maxc == r_ch
    mask_g = (maxc == g_ch) & ~mask_r
    mask_b = ~mask_r & ~mask_g
    hue[mask_r] = 60.0 * (((g_ch[mask_r] - b_ch[mask_r]) / d[mask_r]) % 6.0)
    hue[mask_g] = 60.0 * (((b_ch[mask_g] - r_ch[mask_g]) / d[mask_g]) + 2.0)
    hue[mask_b] = 60.0 * (((r_ch[mask_b] - g_ch[mask_b]) / d[mask_b]) + 4.0)
    blue_mask = (hue >= 190.0) & (hue <= 265.0) & (sat >= 0.20) & (b_ch >= 60.0)
    return float(blue_mask.sum()) / (sample_w * sample_h)


def analyse_signals(frame: np.ndarray, sample_w: int = 80, sample_h: int = 45) -> dict:
    """
    Downsamples frame and detects:
      - blue_ratio: fraction of pixels matching VHS blue
      - black_ratio: fraction of pixels with luma < 22 (blank tape)
      - avg_luma: mean brightness (0-255)
      - is_static: analog static/snow RF noise

    Uses Rust SIMD single-pass when available, falling back to NumPy.
    """
    fh, fw = frame.shape[:2]

    # Fast path: Native Rust single-pass hotpath without NumPy allocations
    if _RUST_AVAILABLE and _RUST_HAS_ANALYSE_SIGNALS and _vhs_core is not None:
        try:
            # We pass contiguous bytes directly to Rust
            buf = frame.tobytes() if not frame.flags["C_CONTIGUOUS"] else frame.data
            res = _vhs_core.analyse_signals(buf, fw, fh, sample_w, sample_h)
            return {
                "blue_ratio": float(res[0]),
                "black_ratio": float(res[1]),
                "avg_luma": float(res[2]),
                "is_static": bool(res[3]),
            }
        except (AttributeError, RuntimeError, TypeError, ValueError) as e:
            logger.debug("Rust analyse_signals failed, falling back to NumPy: %s", e)

    # Fallback path: Nearest downsample + NumPy
    small = cv2.resize(frame, (sample_w, sample_h), interpolation=cv2.INTER_NEAREST)

    # 1. Blue ratio
    if _RUST_AVAILABLE and _vhs_core is not None:
        try:
            blue_ratio = _vhs_core.detect_blue(
                small.tobytes(), sample_w, sample_h, 0.0, sample_w, sample_h
            )
        except (AttributeError, RuntimeError, TypeError, ValueError):
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
    luma_std = float(np.std(luma))
    maxc = np.maximum(np.maximum(r_ch, g_ch), b_ch)
    minc = np.minimum(np.minimum(r_ch, g_ch), b_ch)
    d = maxc - minc + 1e-6
    sat = np.divide(d, maxc, out=np.zeros_like(maxc), where=maxc > 0)
    sat_mean = float(np.mean(sat))
    is_static = bool(
        (luma_std > 45.0)
        and (35.0 <= avg_luma <= 170.0)
        and (sat_mean < 0.25)
        and (blue_ratio < 0.2)
    )

    return {
        "blue_ratio": blue_ratio,
        "black_ratio": black_ratio,
        "avg_luma": avg_luma,
        "is_static": is_static,
    }


def analyse_blue_ratio(frame: np.ndarray, sample_w: int = 80, sample_h: int = 45) -> float:
    """Backwards-compatible wrapper returning only blue ratio."""
    return analyse_signals(frame, sample_w, sample_h)["blue_ratio"]
