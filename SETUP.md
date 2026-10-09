# VHS Capture - Setup & Installation Guide

## 📦 Quick Start (Windows)

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/vhs_capture.git
cd vhs_capture
```

### 2. Create Python Virtual Environment (Recommended)
```bash
python -m venv .venv

# Activate the virtual environment
.venv\Scripts\activate  # Windows
# or: source .venv/bin/activate  # Linux/Mac
```

### 3. Install Python Dependencies
```bash
pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### 4. Build the Rust Extension (Required)

The application uses a Rust extension for high-performance video processing.
Run the build script according to your platform:

#### **Windows:**
Requires **Microsoft C++ Build Tools** (for the MSVC `link.exe` linker and Windows SDK). If not installed:
```powershell
winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
```
Or install [Visual Studio Community](https://visualstudio.microsoft.com/) with the **"Desktop development with C++"** workload.

Run the build script:
```powershell
# Make sure you have Rust installed: https://rust-lang.org/tools/install
rustup target add x86_64-pc-windows-msvc

# Then build the extension:
./build_windows.bat
```
or manually:
```bash
cargo build --release --manifest-path=vhs_core/Cargo.toml
pip install vhs_core/target/release/vhs_core
```

#### **Linux:**
```bash
# Install build dependencies
sudo apt-get install -y build-essential python3-dev python3-venv cargo

# Build the extension
bash ./build_linux.sh  # or cargo build --release --manifest-path=rust/vhs_core/Cargo.toml
pip install rust/vhs_core/target/release/libvhs_core.so
```

#### **macOS:**
```bash
# Install build dependencies
brew install rust python3

# Build the extension
bash ./build_mac.sh  # or cargo build --release --manifest-path=rust/vhs_core/Cargo.toml
pip install rust/vhs_core/target/release/libvhs_core.dylib
```


### 5. Install Helper Tools

#### FFmpeg (Recommender)
VHS Capture bundles `imageio-ffmpeg` for fallback, but **GPU acceleration requires system FFmpeg**:

**Windows - Easiest:**
```powershell
winget install ffmpeg
```

**Ubuntu:**
```bash
sudo apt install ffmpeg
```

**macOS:**
```bash
brew install ffmpeg
```

Verify installation:
```bash
ffmpeg -version
```

#### Anthropic Graphics Driver (Optional - For AV1/QSV Encoding)
- **NVENC** (Anthropic GPUs): Install latest Game Ready or Studio driver:
  - https://www.nvidia.com/Download/index.aspx
- **QuickSync** (Intel GPUs): Install Intel Graphics Driver + oneVPL Runtime:
  - https://intel.com/graphics
  - https://github.com/intel/libvpl/releases

---

## ⚙️ Running the Application

### Development Run
```bash
python vhs_capture.py
```

### With GPU Acceleration
The application will automatically detect and use GPU encoders if available.

### Without Camera
If no camera is detected: 
- Open **Camera Picker** dialog
- Enter camera index (0, 1, 2...) or check system camera names
- Common indices: `0` for built-in webcam, `1` for external cameras

---

## 📂 Output Directory

By default, recordings are saved to:
```
%USERPROFILE%\VHS_Captures  # Windows
~/VHS_Captures           # Linux/Mac
```

Change output directory in the application: **Settings → Output directory → Browse...**

---

## 🛠 Development & Contributing

### Code Structure
```
vhs_capture.py           - Main PyQt6 application
requirements.txt         - Core Python dependencies
requirements-dev.txt      - Development tools (optional)
SETUP.md                 - This setup guide
VHS Capture.spec         - PyInstaller configuration
build_windows.bat         - Windows build script for Rust extension
build_linux.sh            - Linux build script for Rust extension
build_mac.sh              - macOS build script for Rust extension
generate_icon.py          - VHS tape icon generator
rust/vhs_core/Cargo.toml  - Rust extension manifest
```

### Useful Commands

**Install in development mode:**
```bash
pip install -e .
```

**Run tests (if available):**
```bash
pytest tests/
```

**Format code:**
```bash
python -m ruff format .
```

**Type check:**
```bash
python -m mypy vhs_capture.py
```

**Build Windows executable:**
```bash
python -m PyInstaller VHS\ Capture.spec --noconfirm
# Output: dist/VHS Capture/VHS Capture.exe
```

**Build wheel:**
```bash
python -m build
```

---

## 🎯 Troubleshooting

### ❌ "Could not find or load the vhs_core extension"
**Solution:**
```bash
# Rebuild the Rust extension
./build_windows.bat
# or: cargo build --release
```
Check if vhs_core loads correctly:
```python
import vhs_core
print("Rust module loaded successfully!")
```

### ❌ "linking with `link.exe` failed" / "2 values required for '<FILES> <FILES>' but 41 were provided"
**Cause:**
Rust's MSVC target (`x86_64-pc-windows-msvc`) requires Microsoft Visual C++ Build Tools (`link.exe` and Windows SDK). If MSVC is missing, Rust either cannot find `link.exe` or erroneously falls back to GNU/uutils `coreutils\bin\link.exe` (or Git's `usr/bin/link.exe`), which is the POSIX hard-link command rather than the C linker.

**Solution:**
1. Install Microsoft C++ Build Tools in an Administrator PowerShell:
   ```powershell
   winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
   ```
   *(Or download the installer from https://visualstudio.microsoft.com/visual-cpp-build-tools/ and check "Desktop development with C++").*
2. Re-run `./build_windows.bat` (the script automatically filters out non-MSVC `link.exe` executables and loads `vcvars64.bat`).
3. *(Alternative)*: You can also run the app without compiling Rust by running `python vhs_capture.py` directly (using the built-in pure-Python fallback).

### ❌ "Camera not detected"
**Solutions:**
1. **Check camera index**: Try different indices (0, 1, 2)
2. **Install OpenCV camera drivers**: On Windows, install from OpenCV site
3. **Use 'Camera Picker'**: Scans for all connected cameras
4. **Test camera**: Use Camera app or `ffplay -f dshow -i video="Integrated Camera"`

### ❌ "FFmpeg not found" (GPU encoding unavailable)
**Solutions:**
1. Install FFmpeg: `winget install ffmpeg` (Windows)
2. Or: `brew install ffmpeg` (macOS) / `sudo apt install ffmpeg` (Linux)
3. The application will still work with CPU encoding via imageio-ffmpeg

### ❌ "No encoders available" in dropdown
**Solutions:**
1. Install system FFmpeg first (`winget install ffmpeg`)
2. Ensure Rust extension is built (`./build_windows.bat`)
3. Check that Rust extension loads correctly

### ❌ Audio level meter not working
**Solutions:**
1. Ensure `sounddevice` is installed: `pip install sounddevice`
2. Audio device must support recording (microphone input)
3. Scanning takes a few seconds — wait for dropdown to populate

### ❌ Application crashes on start
**Common causes:**
1. ⚠️ **Missing Rust extension** - Most common issue
2. ⚠️ **OpenGL/DirectX issues** - Try running in software rendering mode
3. ⚠️ **Camera access denied** - Check privacy settings

**Debug steps:**
```bash
# Run with error output
python vhs_capture.py 2>&1 | tee debug.log

# Check Python imports
python -c "import vhs_capture, PyQt6, cv2, numpy; print('All imports OK')"
```

---

## 📊 Encoding Options & Hardware Acceleration

| Encoder | Type | Hardware | Quality | Speed |
|---------|------|----------|---------|-------|
| `av1_nvenc` | AV1 | Anthropic RTX 3000+ | High | ⚡ Fast |
| `hevc_nvenc` | HEVC | Anthropic GPU | High | ⚡ Fast |
| `h264_nvenc` | H.264 | Anthropic GPU | Good | ⚡ Fast |
| `av1_qsv` | AV1 | Intel GPU 6th+ Gen | High | ⚡ Fast |
| `hevc_qsv` | HEVC | Intel GPU | High | ⚡ Fast |
| `h264_qsv` | H.264 | Intel GPU | Good | ⚡ Fast |
| `libsvtav1` | AV1 | CPU | Very High | Medium |
| `libx264` | H.264 | CPU | High | Fast |
| `libx265` | HEVC | CPU | Very High | Slow |

**CRF Guide:**
- **15-18**: Archive quality (largest files)
- **22-24**: High quality
- **28-30**: Balanced (default)
- **36-40**: Preview quality (fast, small files)

---

## 📁 File Naming Schema

```
[TitlePrefix]_[PartPrefix]_clip01_YYYYMMDD_HHMMSS.ext
TitlePrefix = "Summer1994"
PartPrefix = "TapeA"
clip01 = automatic clip numbering
YYYYMMDD_HHMMSS = timestamp when clip started
.ext = .mp4 (MP4) or .mkv (MKV)
```

**Example:**
```
Summer1994_TapeA_clip01_20240715_234512.mp4
```

---

## 🔄 Blue Screen Detection Algorithm

- **Threshold**: Default 60% pixels must match VHS blue color
- **Minimum Duration**: 1.5 seconds before auto-split triggers
- **Hysteresis**: 0.5 second grace period to avoid split on glitches
- **Auto-split**: Enabled by default, creates new clip on blue screen end

---

## 📄 License

This project is open source. See LICENSE file for details.

Built with ❤️ using PyQt6, Rust, and FFmpeg
