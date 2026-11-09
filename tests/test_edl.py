"""
Unit tests for SMPTE timecodes, CMX 3600 EDL, and FFmpeg chapters export.
"""

import pytest
from vhs_lib.edl import (
    frames_to_tc,
    generate_chapters,
    generate_edl,
    tc_to_frames,
)


def test_frames_to_tc_pal_25fps():
    assert frames_to_tc(0, 25.0) == "00:00:00:00"
    assert frames_to_tc(12, 25.0) == "00:00:00:12"
    assert frames_to_tc(25, 25.0) == "00:00:01:00"
    assert frames_to_tc(25 * 60, 25.0) == "00:01:00:00"
    assert frames_to_tc(25 * 3600, 25.0) == "01:00:00:00"
    assert frames_to_tc(25 * 3661 + 7, 25.0) == "01:01:01:07"


def test_frames_to_tc_ntsc_30fps():
    assert frames_to_tc(0, 29.97) == "00:00:00:00"
    assert frames_to_tc(30, 29.97) == "00:00:01:00"
    assert frames_to_tc(30 * 60, 29.97) == "00:01:00:00"


def test_tc_roundtrip():
    fps = 25.0
    for f in [0, 1, 24, 25, 120, 1500, 90000, 123456]:
        tc = frames_to_tc(f, fps)
        recovered = tc_to_frames(tc, fps)
        assert recovered == f


def test_tc_to_frames_invalid_format():
    with pytest.raises(ValueError, match="Invalid timecode format"):
        tc_to_frames("00:00:00", 25.0)


def test_generate_edl():
    segments = [
        ("C:/recordings/clip01.mp4", 10.0),  # 250 frames @ 25fps
        ("C:/recordings/clip02.mp4", 5.0),   # 125 frames @ 25fps
    ]
    edl = generate_edl("My Summer Vacation", segments, fps=25.0)

    assert "TITLE: My_Summer_Vacation" in edl
    assert "FCM: NON-DROP FRAME" in edl
    assert "001  AX       V     C        00:00:00:00 00:00:10:00 00:00:00:00 00:00:10:00" in edl
    assert "* FROM CLIP NAME: clip01.mp4" in edl
    assert "002  AX       V     C        00:00:00:00 00:00:05:00 00:00:10:00 00:00:15:00" in edl
    assert "* FROM CLIP NAME: clip02.mp4" in edl


def test_generate_chapters():
    segments = [
        ("clip01.mp4", 12.5),
        ("clip02.mp4", 8.0),
    ]
    chapters = generate_chapters(segments)

    assert ";FFMETADATA1" in chapters
    assert "[CHAPTER]" in chapters
    assert "TIMEBASE=1/1000" in chapters
    assert "START=0" in chapters
    assert "END=12500" in chapters
    assert "title=Part 1 - clip01.mp4" in chapters
    assert "START=12500" in chapters
    assert "END=20500" in chapters
    assert "title=Part 2 - clip02.mp4" in chapters

