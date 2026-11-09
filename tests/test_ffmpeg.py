"""
Unit tests for FFmpeg command building, hardware error translation, and writer thread.
"""

from unittest.mock import MagicMock, patch

from vhs_lib.ffmpeg import (
    FFmpegWriter,
    _friendly_hw_error,
    _WriterThread,
)


def test_friendly_hw_error_nvenc():
    err1 = "Error while opening encoder: Driver does not support the required nvenc API version"
    msg1 = _friendly_hw_error("h264_nvenc", err1)
    assert "GPU too old" in msg1 or "driver" in msg1.lower()

    err2 = "Cannot load nvcuda.dll"
    msg2 = _friendly_hw_error("h264_nvenc", err2)
    assert "NVIDIA driver not found" in msg2


def test_friendly_hw_error_qsv():
    err = "Error creating a MFX session: -9"
    msg = _friendly_hw_error("h264_qsv", err)
    assert "MFX session failed" in msg


def test_ffmpeg_writer_command_line_filters():
    writer = FFmpegWriter(
        output_path="test_out.mp4",
        width=720,
        height=576,
        fps=25.0,
        encoder="libsvtav1",
        crf=26,
        aspect_ratio="4:3",
        deinterlace="bwdif",
        mask_head_switching=True,
    )

    with patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.stdin = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.poll.return_value = None
        mock_popen.return_value = mock_proc

        writer.start()

        args, _ = mock_popen.call_args
        cmd = args[0]

        # Check rawvideo input settings
        assert "-f" in cmd and "rawvideo" in cmd
        assert "-s" in cmd and "720x576" in cmd
        assert "-r" in cmd and "25.0" in cmd
        assert "-pix_fmt" in cmd and "bgr24" in cmd

        # Check filterchain: bwdif deinterlacing + drawbox head switching
        vf_idx = cmd.index("-vf")
        filters = cmd[vf_idx + 1]
        assert "drawbox=y=ih-12:h=12:color=black:t=fill" in filters
        assert "bwdif=mode=0:parity=-1:deint=1" in filters

        # Check codec and output flags
        assert "-c:v" in cmd and "libsvtav1" in cmd
        assert "-movflags" in cmd and "+faststart" in cmd
        assert cmd[-1] == "test_out.mp4"


def test_writer_thread_broken_pipe_reporting():
    errors_received = []

    def on_error(errs):
        errors_received.extend(errs)

    thread = _WriterThread(capacity=4, on_error=on_error)

    # Create mock writer that reports write failure
    mock_writer = MagicMock()
    mock_writer.write_raw.return_value = False
    mock_writer.last_errors.return_value = ["Encoder out of memory"]

    thread.set_writer(mock_writer)

    import numpy as np
    dummy_frame = np.zeros((10, 10, 3), dtype=np.uint8)
    thread.push(dummy_frame)

    thread.start()
    import time
    time.sleep(0.15)
    thread.stop()

    assert len(errors_received) > 0
    assert "Encoder out of memory" in errors_received[0]
