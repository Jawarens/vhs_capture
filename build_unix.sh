#!/usr/bin/env bash
set -e

echo "============================================================"
echo " VHS Capture — Unix build (Python + Rust + FFmpeg)"
echo "============================================================"

pip install --upgrade maturin pyinstaller PyQt6 opencv-python numpy sounddevice imageio-ffmpeg

export PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1
if command -v cargo >/dev/null 2>&1; then
    echo "Building Rust extension (vhs_core)..."
    (cd vhs_core && maturin build --release)
    pip install --force-reinstall vhs_core/target/wheels/*.whl
else
    echo "WARNING: Rust/cargo not found — building without vhs_core native extension."
fi

pyinstaller \
    --onefile \
    --windowed \
    --name "VHSCapture" \
    --add-data "README.md:." \
    --add-data "vhs_capture.ico:." \
    --icon "vhs_capture.ico" \
    --hidden-import "cv2" \
    --hidden-import "numpy" \
    --hidden-import "vhs_core" \
    --hidden-import "sounddevice" \
    --hidden-import "imageio_ffmpeg" \
    --hidden-import "PyQt6.QtCore" \
    --hidden-import "PyQt6.QtGui" \
    --hidden-import "PyQt6.QtWidgets" \
    --collect-all "cv2" \
    --collect-all "vhs_core" \
    --collect-all "sounddevice" \
    --collect-all "imageio_ffmpeg" \
    vhs_capture.py

echo ""
echo "Build complete → dist/VHSCapture"
echo "NOTE: FFmpeg is bundled via imageio-ffmpeg; install system ffmpeg for hardware GPU encoding."
