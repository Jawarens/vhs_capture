@echo off
setlocal enabledelayedexpansion
echo ============================================================
echo  VHS Capture -- Windows build (Python + Rust extension)
echo ============================================================
echo.

:: ── Find Python ────────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause & exit /b 1
)
:: Capture the python executable path so all tools use the same one
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set PYTHON=%%p
echo Python: !PYTHON!

:: ── Find/install Rust ──────────────────────────────────────────────
set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
cargo --version >nul 2>&1
if errorlevel 1 (
    echo Rust not found. Downloading rustup...
    curl --proto "=https" --tlsv1.2 -sSf -o rustup-init.exe https://win.rustup.rs/x86_64
    if errorlevel 1 (
        echo ERROR: Could not download rustup. Check your internet connection.
        pause & exit /b 1
    )
    rustup-init.exe -y --default-toolchain stable --no-modify-path
    del rustup-init.exe
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    cargo --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Rust install failed.
        echo  Please install manually from https://rustup.rs then re-run this script.
        pause & exit /b 1
    )
    echo Rust installed OK.
) else (
    for /f "tokens=2" %%v in ('cargo --version') do echo Rust %%v found.
)

:: ── Check FFmpeg ───────────────────────────────────────────────────
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo WARNING: FFmpeg not found on PATH.
    echo   Option 1 ^(recommended^): winget install Gyan.FFmpeg
    echo   Option 2: download from https://www.gyan.dev/ffmpeg/builds/
    echo             extract and add the bin\ folder to your system PATH.
    echo   The app will open but cannot record without FFmpeg.
    echo.
) else (
    for /f "tokens=3" %%v in ('ffmpeg -version 2^>^&1 ^| findstr "ffmpeg version"') do echo FFmpeg %%v found.
)

:: ── Install Python deps via the exact python we found ──────────────
echo.
echo Installing Python dependencies...
"!PYTHON!" -m pip install --upgrade maturin PyQt6 opencv-python numpy pyinstaller sounddevice
if errorlevel 1 (
    echo ERROR: pip install failed.
    pause & exit /b 1
)
echo   Done.

:: ── Build Rust extension using "python -m maturin" ─────────────────
:: We use "python -m maturin" instead of "maturin" directly because
:: pip installs scripts into a user-local Scripts folder that may not
:: be on PATH in this session.
echo.
echo Building Rust extension (vhs_core)...
cd vhs_core

"!PYTHON!" -m maturin build --release
if errorlevel 1 (
    echo.
    echo ERROR: Rust build failed.
    echo  Common causes:
    echo    - Rust toolchain not on PATH  ^(re-open this window after rustup install^)
    echo    - Missing Visual Studio C++ build tools
    echo      Install from: https://visualstudio.microsoft.com/visual-cpp-build-tools/
    echo      Select "Desktop development with C++"
    cd ..
    pause & exit /b 1
)

:: Find and install the wheel
for /f "delims=" %%f in ('dir /b /s "target\wheels\vhs_core*.whl" 2^>nul') do set WHEEL=%%f
if "!WHEEL!"=="" (
    echo ERROR: No wheel file found after build.
    cd ..
    pause & exit /b 1
)
echo Installing !WHEEL!...
"!PYTHON!" -m pip install "!WHEEL!" --force-reinstall
if errorlevel 1 (
    echo ERROR: Wheel install failed.
    cd ..
    pause & exit /b 1
)
cd ..
echo   Rust extension installed.

:: ── Verify extension loads ─────────────────────────────────────────
echo.
echo Verifying vhs_core...
"!PYTHON!" -c "import vhs_core; q=vhs_core.FrameQueue(4); r=vhs_core.detect_blue(bytes(64*64*3),64,64,0.5,16,16); print('  OK -- FrameQueue, detect_blue, bgr_to_rgb all working')"
if errorlevel 1 (
    echo   WARNING: vhs_core verification failed. App will run without Rust acceleration.
) else (
    echo   vhs_core OK.
)

:: ── Bundle as standalone exe ───────────────────────────────────────
echo.
echo Building standalone executable...
"!PYTHON!" -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "VHS Capture" ^
    --add-data "README.md;." ^
    --hidden-import "cv2" ^
    --hidden-import "numpy" ^
    --hidden-import "sounddevice" ^
    --hidden-import "vhs_core" ^
    --hidden-import "PyQt6.QtCore" ^
    --hidden-import "PyQt6.QtGui" ^
    --hidden-import "PyQt6.QtWidgets" ^
    --collect-all "cv2" ^
    --collect-all "vhs_core" ^
    --collect-all "sounddevice" ^
    vhs_capture.py

if errorlevel 1 (
    echo.
    echo BUILD FAILED. See errors above.
    pause & exit /b 1
)

echo.
echo ============================================================
echo  Build complete!
echo.
echo  Executable:  dist\VHS Capture.exe
echo  Rust boost:  vhs_core active
echo.
echo  FFmpeg must be on PATH or placed next to the .exe
echo  GPU encoders ^(NVENC / QSV^) are auto-detected at launch
echo ============================================================
pause
