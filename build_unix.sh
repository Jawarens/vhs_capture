#!/usr/bin/env bash
set -e

echo "============================================================"
echo " VHS Capture — build"
echo "============================================================"

pip install --upgrade pyinstaller PyQt6 opencv-python numpy

pyinstaller \
    --onefile \
    --windowed \
    --name "VHSCapture" \
    --hidden-import "cv2" \
    --hidden-import "numpy" \
    --hidden-import "PyQt6.QtCore" \
    --hidden-import "PyQt6.QtGui" \
    --hidden-import "PyQt6.QtWidgets" \
    --collect-all "cv2" \
    vhs_capture.py

echo ""
echo "Build complete → dist/VHSCapture"
echo "NOTE: FFmpeg must be installed (brew install ffmpeg  /  apt install ffmpeg)"
