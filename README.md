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

✅ **Precision A/V Sync & Drop Protection:** Real-time wall-clock sync inserting duplicate frames when capture cards stutter, plus `aresample=async=1000` to eliminate audio drift
✅ **VHS Native Standards:** True 4:3 SD profiles for PAL (720×576 @ 25fps) and NTSC (720×480 @ 29.97fps) with correct aspect ratio tags
✅ **Motion-Adaptive Deinterlacing:** Integrated `bwdif` (standard and 50p/60p smooth motion) and `yadif` filters
✅ **Head-Switching Noise Masking:** Eliminates the flickering static band in the bottom 12 scanlines with a clean black bar
✅ **Multi-Signal Auto-Split:** Detects VHS blue screen, blank black screen (commercial/cut marks), and analog static noise
✅ **Unattended Tape-End Auto-Stop:** Automatically stops and finalizes recording when blank screen and audio silence persist
✅ **DirectShow Hardware Settings:** Dedicated dialog button to switch between Composite and S-Video and adjust hardware Proc Amp
✅ **Audio Enhancements:** Mono-to-stereo channel duplication (Left to both), 50/60Hz mains ground hum filter, and EBU R128 `loudnorm`
✅ **Lossless Archival Master:** Full support for `FFV1` with lossless FLAC audio in MKV alongside modern AV1, HEVC, and H.264
✅ **Real-Time Disk Space Guard:** Live free disk space and recording time monitor with emergency auto-stop under 1.5 GB
✅ **Frame Snapshot Capture:** Instant still image grab (`Ctrl+S`) saved directly to PNG
✅ **CMX 3600 EDL & Chapter Export:** Generate Edit Decision Lists and MKV chapters for video editing software and media servers
✅ **Hardware Acceleration:** NVENC (RTX 3000+) and QuickSync (Intel 6th+ Gen) support
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

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Space` | Toggle Start / Stop recording |
| `Return` / `Enter` | Force manual segment split |
| `Ctrl+S` | Save full-resolution frame snapshot (PNG) |
| `Ctrl+O` | Open camera picker dialog |

---

## Settings

| Setting | Description |
|---|---|
| Camera index | 0 = default webcam, 1/2 = USB capture card |
| Resolution | PAL (720×576 4:3), NTSC (720×480 4:3), HD (1280×720), or Full HD (1920×1080) |
| FPS | 25 for PAL tapes, 29.97 for NTSC |
| Deinterlace | `bwdif` (25/30p), `bwdif 2x` (smooth 50/60p), or `yadif` |
| Head-switching mask | Replaces noisy bottom 12 scanlines with solid black |
| Audio Channels | Stereo, Mono (L duplicated to both), Mono (R duplicated to both) |
| Hum Filter | 65Hz highpass filter removing 50Hz/60Hz ground-loop mains hum |
| Normalize Audio | EBU R128 `loudnorm` standard loudness leveling |
| Encoder | NVENC (AV1/HEVC/H264), QuickSync, SVT-AV1, x265, x264, or Lossless FFV1 |
| Quality (CRF) | Lower = higher quality. 15=Archive, 24=High, 28=Balanced |
| Blue / Black / Static | Configurable threshold and duration for auto-splitting |
| Tape End Auto-Stop | Stops recording when blank screen + audio silence persist (default 30s) |

---

## Tips

- **S-Video vs Composite**: Click **"Device settings"** to switch your capture card between Composite (Yellow RCA) and S-Video. S-Video provides significantly cleaner color with zero dot crawl.
- **Interlacing**: Analog VHS is interlaced. Enable `bwdif 2x` to deinterlace 50 fields/sec into 50 progressive frames per second for ultra-smooth tape playback.
- **PAL vs NTSC**: Choose 720×576 @ 25fps for European PAL tapes, or 720×480 @ 29.97fps for US/Japanese NTSC tapes.
- **Preservation Masters**: For true archival preservation before editing or restoration, select the **FFV1 Lossless Archival Master** profile.

