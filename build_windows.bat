@echo off
setlocal enabledelayedexpansion

set LOGFILE=%~dp0build_log.txt
echo Build started %date% %time% > "%LOGFILE%"

echo ==============================================================
echo VHS Capture -- Windows build (Python + Rust + FFmpeg)
echo Log: %LOGFILE%
echo ==============================================================
echo.

:: ── Find Python and add its user Scripts dir to PATH ─────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python not found. Install from https://python.org
    pause & exit /b 1
)
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set "PYEXE=%%p"
for /f "delims=" %%p in ('python -c "import sysconfig; print(sysconfig.get_path(\"scripts\",\"nt_user\"))"') do set "PYUSERSCRIPTS=%%p"
for /f "delims=" %%p in ('python -c "import sysconfig; print(sysconfig.get_path(\"scripts\"))"') do set "PYSCRIPTS=%%p"

:: Prepend user Scripts + cargo to PATH so maturin/pyinstaller are reachable
set "PATH=%PYUSERSCRIPTS%;%PYSCRIPTS%;%USERPROFILE%\.cargo\bin;%PATH%"

echo Python: %PYEXE%
echo Scripts: %PYUSERSCRIPTS%
echo.

:: ── Rust ──────────────────────────────────────────────────────────
cargo --version >nul 2>&1
if errorlevel 1 (
    echo Rust not found. Downloading rustup-init...
    curl --proto "=https" --tlsv1.2 -sSf -o rustup-init.exe https://win.rustup.rs/x86_64
    if errorlevel 1 (
        echo ERROR: Could not download rustup. Check your internet.
        pause & exit /b 1
    )
    rustup-init.exe -y --default-toolchain stable --no-modify-path
    del rustup-init.exe
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    cargo --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Rust install failed. Install from https://rustup.rs
        pause & exit /b 1
    )
    echo Rust installed.
) else (
    echo Rust found.
)

:: ── Clean stale PyInstaller/Python caches ────────────────────────────
echo.
echo Cleaning stale build caches...
if exist "%~dp0build" (
    rmdir /s /q "%~dp0build"
    echo Removed build\
)
if exist "%~dp0dist" (
    rmdir /s /q "%~dp0dist"
    echo Removed dist\
)
:: Remove .pyc files so PyInstaller always sees fresh source
for /r "%~dp0" %%f in (*.pyc) do del "%%f" >nul 2>&1
if exist "%~dp0__pycache__" rmdir /s /q "%~dp0__pycache__"
echo Done.

:: ── Install Python dependencies ──────────────────────────────────
echo.
echo Installing Python dependencies...
python -m pip install --user --upgrade ^
    maturin PyQt6 opencv-python numpy ^
    sounddevice imageio-ffmpeg ^
    pyinstaller >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo ERROR: pip install failed. See %LOGFILE%
    pause & exit /b 1
)
echo Done.

:: ── Find maturin (handle --user install fallback) ─────────────────
maturin --version >nul 2>&1
if errorlevel 1 (
    set "MATURIN=python -m maturin"
    echo maturin via: python -m maturin
) else (
    set "MATURIN=maturin"
    echo maturin via: maturin
)

:: ── Build Rust extension ──────────────────────────────────────────
echo.
echo Building Rust extension (vhs_core)...
cd "%~dp0vhs_core"
%MATURIN% build --release >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Rust build failed. See %LOGFILE%
    echo.
    powershell -Command "Get-Content '%LOGFILE%' -Tail 30"
    cd "%~dp0"
    pause & exit /b 1
)

:: Install the wheel
set "WHEEL="
for /f "delims=" %%f in ('dir /b /s "target\wheels\*.whl" 2^>nul') do set "WHEEL=%%f"
if "%WHEEL%"=="" (
    echo ERROR: No wheel produced. See %LOGFILE%
    cd "%~dp0"
    pause & exit /b 1
)
echo Installing: %WHEEL%
python -m pip install --user --force-reinstall "%WHEEL%" >> "%LOGFILE%" 2>&1
cd "%~dp0"
echo vhs_core installed.

:: ── Verify vhs_core loads ─────────────────────────────────────────
echo.
python -c "import vhs_core; print(' vhs_core OK')"
if errorlevel 1 (
    echo WARNING: vhs_core failed to import -- app runs in fallback mode.
)

:: ── Verify FFmpeg (warn only -- imageio-ffmpeg provides a fallback) ──
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo.
    echo NOTE: FFmpeg not found on PATH.
    echo imageio-ffmpeg will provide a bundled binary for recording.
    echo For hardware GPU encoding install FFmpeg: winget install ffmpeg
    echo.
) else (
    echo FFmpeg found on PATH.
)

:: ── Build standalone exe ──────────────────────────────────────────
echo.
echo Building standalone executable...

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "VHS Capture" ^
    --add-data "README.md;." ^
    --add-data "vhs_capture.ico;." ^
    --icon "vhs_capture.ico" ^
    --hidden-import "cv2" ^
    --hidden-import "numpy" ^
    --hidden-import "vhs_core" ^
    --hidden-import "sounddevice" ^
    --hidden-import "imageio_ffmpeg" ^
    --hidden-import "PyQt6.QtCore" ^
    --hidden-import "PyQt6.QtGui" ^
    --hidden-import "PyQt6.QtWidgets" ^
    --collect-all "cv2" ^
    --collect-all "vhs_core" ^
    --collect-all "sounddevice" ^
    --collect-all "imageio_ffmpeg" ^
    vhs_capture.py >> "%LOGFILE%" 2>&1

if errorlevel 1 (
    echo ERROR: PyInstaller failed. See %LOGFILE%
    echo.
    powershell -Command "Get-Content '%LOGFILE%' -Tail 40"
    pause & exit /b 1
)

echo.
echo ============================================================
echo BUILD COMPLETE
echo.
echo Executable : dist\VHS Capture.exe
echo.
echo FFmpeg : bundled via imageio-ffmpeg (libsvtav1/libx264)
echo For GPU encoding (NVENC/QSV) also install:
echo    winget install ffmpeg
echo ============================================================
pause
