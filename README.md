# VHS Capture - VHS Tape Recorder & Auto-Splitter

🎬 Record from webcam to modern AV1/HEVC with automatic tape splitting on blue screen detection

![Application Screenshot](placeholder.png)

> Capture, detect, and split VHS tapes automatically with professional quality encoding!

## Requirements

- Python 3.10+
- FFmpeg with AV1 support (libsvtav1 recommended)


## 📋 Quick Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Build Rust extension (REQUIRED for best performance)
./build_windows.bat  # Windows
# or: bash build_linux.sh    # Linux
# or: bash build_mac.sh      # macOS

# Run application
python vhs_capture.py

# Build Windows executable
python -m PyInstaller "VHS Capture.spec" --noconfirm
```

## 🎯 Features

✅ **Modern Codecs:** AV1, HEVC, H.264 with CRF quality controls
✅ **Hardware Acceleration:** NVENC (RTX 3000+) and QuickSync (Intel 6th+ Gen) support
✅ **Auto-Split:** Detects VHS blue screens and splits video automatically
✅ **Audio Meter:** Real-time microphone level monitoring
✅ **Tape Timer:** Optional automatic stop after configured duration (30m/45m/60m/90m/120m)
✅ **Multi-Platform:** Windows, Linux, macOS (via PyQt6 and Rust)

## 📖 Documentation

- **Full Setup Guide:** `SETUP.md` - Complete installation instructions
- **Dependency Verification:** `verify.py` - Quick installation check
- **Build System:** `VHS Capture.spec` (PyInstaller config for Windows executable)
- **Icon Design:** `generate_icon.py` - Custom VHS tape icon generator



### Windows

1. **Install Python** from https://python.org (check "Add to PATH")

2. **Install FFmpeg**  
   Download a full build from https://www.gyan.dev/ffmpeg/builds/  
   Extract and add the `bin` folder to your system PATH.  
   Or with winget: `winget install ffmpeg`

3. **Install Python dependencies**
   ```
   pip install PyQt6 opencv-python numpy
   ```

4. **Run**
   ```
   python vhs_capture.py
   ```

---

### macOS

1. **Install Homebrew** (if not already): https://brew.sh

2. **Install FFmpeg and Python**
   ```
   brew install ffmpeg python
   ```

3. **Install Python dependencies**
   ```
   pip3 install PyQt6 opencv-python numpy
   ```

4. **Run**
   ```
   python3 vhs_capture.py
   ```

---

### Linux (Ubuntu/Debian)

1. **Install FFmpeg**
   ```
   sudo apt update && sudo apt install ffmpeg
   ```

2. **Install Python dependencies**
   ```
   pip install PyQt6 opencv-python numpy
   ```

3. **Run**
   ```
   python3 vhs_capture.py
   ```

---

## Usage

1. **Open camera** — select your camera index (0 = first/default camera, 1 = second, etc.)
2. **Start recording** — captures video continuously; output goes to `~/VHS_Captures/` by default
3. **Blue screen detection** — when a blue screen is detected for the minimum duration, the current clip is saved and a new one starts automatically
4. **Manual split** — press this to force a segment split at any time
5. **Stop recording** — finalises all clips

---

## Settings

| Setting | Description |
|---|---|
| Camera index | 0 = first webcam, 1 = second, etc. Your capture card usually appears as 1 or 2 |
| Resolution | Match your capture card's output (1280×720 is a good default for VHS) |
| FPS | 25 for PAL tapes (Europe), 30 for NTSC (US/Japan) |
| Encoder | `libsvtav1` is fastest AV1; `libaom-av1` is best quality but slow |
| Quality (CRF) | Lower = better quality, larger files. 24–32 is a good range for VHS |
| Blue threshold | % of pixels that must be blue to trigger detection. Lower if your tape's blue screen is dark/grey-blue |
| Min blue duration | How long the blue screen must persist before it counts (avoids false triggers on brief colour bursts) |

---

## Tips

- **Capture card**: if you're using a USB capture card, it will show up as a camera. Try index 1 or 2 if 0 is your webcam
- **PAL vs NTSC**: set FPS to 25 for European VHS tapes, 29.97 or 30 for US/Japanese
- **Blue screen colour**: some VHS decks show a darker or slightly grey-blue. Lower the blue threshold to ~45–50% if it isn't being detected
- **AV1 playback**: VLC, MPV, and most modern players support AV1. Windows 11 supports it natively
