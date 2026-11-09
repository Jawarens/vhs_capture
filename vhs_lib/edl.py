"""
SMPTE Timecode conversion, CMX 3600 EDL export, and FFmpeg/MKV chapter generation.
"""

from __future__ import annotations

from pathlib import Path


def frames_to_tc(total_frames: int, fps: float = 25.0) -> str:
    """Convert an integer frame count to SMPTE non-drop timecode (HH:MM:SS:FF)."""
    fps_int = max(1, round(fps))
    frames = max(0, int(total_frames))
    ff = frames % fps_int
    total_sec = frames // fps_int
    ss = total_sec % 60
    mm = (total_sec // 60) % 60
    hh = total_sec // 3600
    return f"{hh:02d}:{mm:02d}:{ss:02d}:{ff:02d}"


def tc_to_frames(tc: str, fps: float = 25.0) -> int:
    """Convert SMPTE timecode (HH:MM:SS:FF) back to total frame count."""
    fps_int = max(1, round(fps))
    parts = tc.strip().split(":")
    if len(parts) != 4:
        raise ValueError(f"Invalid timecode format (expected HH:MM:SS:FF): '{tc}'")
    hh, mm, ss, ff = (int(p) for p in parts)
    return ((hh * 3600 + mm * 60 + ss) * fps_int) + ff


def generate_edl(base_title: str, segments: list[tuple[str, float]], fps: float = 25.0) -> str:
    """
    Generate CMX 3600 Edit Decision List (EDL) text for recorded segments.
    Each segment is a tuple of (file_path_or_name, duration_in_seconds).
    """
    clean_title = (base_title or "VHS_Tape").replace(" ", "_")
    fps_val = max(1.0, float(fps))

    edl_lines = [
        f"TITLE: {clean_title}",
        "FCM: NON-DROP FRAME",
        "",
    ]
    curr_frame = 0

    for i, (path_str, dur_sec) in enumerate(segments, start=1):
        name = Path(path_str).name
        clip_frames = max(1, round(dur_sec * fps_val)) if dur_sec > 0 else 0
        src_in = frames_to_tc(0, fps_val)
        src_out = frames_to_tc(clip_frames, fps_val)
        rec_in = frames_to_tc(curr_frame, fps_val)
        rec_out = frames_to_tc(curr_frame + clip_frames, fps_val)
        curr_frame += clip_frames

        edl_lines.append(f"{i:03d}  AX       V     C        {src_in} {src_out} {rec_in} {rec_out}")
        edl_lines.append(f"* FROM CLIP NAME: {name}")
        edl_lines.append("")

    return "\n".join(edl_lines)


def generate_chapters(segments: list[tuple[str, float]]) -> str:
    """
    Generate standard FFmpeg / MKV metadata chapters format (;FFMETADATA1).
    """
    ch_lines = [";FFMETADATA1"]
    curr_ms = 0
    for i, (path_str, dur_sec) in enumerate(segments, start=1):
        name = Path(path_str).name
        end_ms = curr_ms + round(dur_sec * 1000)
        ch_lines.append("[CHAPTER]")
        ch_lines.append("TIMEBASE=1/1000")
        ch_lines.append(f"START={curr_ms}")
        ch_lines.append(f"END={end_ms}")
        ch_lines.append(f"title=Part {i} - {name}")
        ch_lines.append("")
        curr_ms = end_ms

    return "\n".join(ch_lines)
