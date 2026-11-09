"""
Video capture device probing and DirectShow / V4L2 camera enumeration.
"""

from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import cv2

logger = logging.getLogger(__name__)

PROBE_RESOLUTIONS = [
    (1920, 1080),
    (1280, 720),
    (720, 576),  # PAL
    (720, 480),  # NTSC
    (640, 480),
    (320, 240),
]


def _probe_camera(index: int) -> dict | None:
    """Probe a single camera index for maximum supported resolution and backend."""
    backend = cv2.CAP_DSHOW if sys.platform == "win32" else cv2.CAP_ANY
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened() and backend != cv2.CAP_ANY:
        cap = cv2.VideoCapture(index)
    if not cap.isOpened():
        return None

    best_w, best_h = 0, 0
    for tw, th in PROBE_RESOLUTIONS:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, tw)
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


def _enumerate_cameras(max_index: int = 8) -> list[dict]:
    """Scan cameras up to max_index in parallel."""
    found: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(4, max_index)) as ex:
        futures = {ex.submit(_probe_camera, i): i for i in range(max_index)}
        for fut in as_completed(futures):
            try:
                info = fut.result()
                if info:
                    found.append(info)
            except (cv2.error, OSError, RuntimeError) as e:
                logger.debug("Error probing camera index: %s", e)
    # Sort by index
    found.sort(key=lambda x: x["index"])
    return found
