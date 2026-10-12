@echo off
setlocal enabledelayedexpansion

:: CI guard: when running in CI (Actions) the CI variable will be defined.
:: Use this to skip interactive pauses and to avoid --user pip installs.
set "PIP_USER=--user"
if defined CI set "PIP_USER="

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
    if not defined CI (
        pause
    )
    exit /b 1
)
for /f "delims=" %%p in ('python -c "import sys; print(sys.executable)"') do set "PYEXE=%%p"
for /f "delims=" %%p in ('python -c "import sysconfig; print(sysconfig.get_path(\"scripts\",\"nt_user\"))"') do set "PYUSERSCRIPTS=%%p"
for /f "delims=" %%p in ('python -c "import sysconfig; print(sysconfig.get_path(\"scripts\"))"') do set "PYSCRIPTS=%%p"

:: Prepend user Scripts + cargo to PATH so maturin/pyinstaller are reachable
set "PATH=%PYUSERSCRIPTS%;%PYSCRIPTS%;%USERPROFILE%\.cargo\bin;%PATH%"

:: Remove coreutils / Git usr\bin from PATH so GNU link.exe does not shadow MSVC linker
set "PATH=!PATH:C:\Program Files\coreutils\bin;=!"
set "PATH=!PATH:C:\Program Files\coreutils\bin=!"
set "PATH=!PATH:C:\Program Files\Git\usr\bin;=!"
set "PATH=!PATH:C:\Program Files\Git\usr\bin=!"

echo Python: %PYEXE%
echo Scripts: %PYUSERSCRIPTS%
echo.

:: ── Detect MSVC C++ Build Tools ────────────────────────────────────
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
set "VCVARS="
if exist "!VSWHERE!" (
    for /f "usebackq tokens=*" %%i in (`"!VSWHERE!" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do (
        if exist "%%i\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
    )
)
if defined VCVARS (
    echo Initializing MSVC environment: !VCVARS!
    call "!VCVARS!" >nul 2>&1
)

:: ── Rust ──────────────────────────────────────────────────────────
cargo --version >nul 2>&1
if errorlevel 1 (
    echo Rust not found. Downloading rustup-init...
    curl --proto "=https" --tlsv1.2 -sSf -o rustup-init.exe https://win.rustup.rs/x86_64
    if errorlevel 1 (
        echo ERROR: Could not download rustup. Check your internet.
        if not defined CI (
            pause
        )
        exit /b 1
    )
    rustup-init.exe -y --default-toolchain stable --no-modify-path
    del rustup-init.exe
    set "PATH=%USERPROFILE%\.cargo\bin;%PATH%"
    cargo --version >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Rust install failed. Install from https://rustup.rs
        if not defined CI (
            pause
        )
        exit /b 1
    )
    echo Rust installed.
) else (
    echo Rust found.
)

:: Check for MSVC linker
set "NEED_MSVC=0"
where link.exe >nul 2>&1
if errorlevel 1 (
    set "NEED_MSVC=1"
) else (
    :: Verify link.exe is not GNU/uutils link (which accepts --version)
    link.exe --version >nul 2>&1
    if not errorlevel 1 set "NEED_MSVC=1"
)
if "!NEED_MSVC!"=="0" goto msvc_ok

echo.
echo ==============================================================
echo WARNING: Microsoft Visual C++ Build Tools not found.
echo Rust requires the MSVC linker [link.exe] and Windows SDK.
echo.
echo To install, run this in an Administrator PowerShell:
echo   winget install --id Microsoft.VisualStudio.2022.BuildTools --override "--passive --wait --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended"
echo.
echo Or install Visual Studio with "Desktop development with C++".
echo.
echo NOTE: You can run VHS Capture directly without compiling Rust:
echo   python vhs_capture.py
echo ==============================================================
echo.
if not defined CI (
    pause
)
exit /b 1

:msvc_ok

:: ── Clean stale PyInstaller/Python caches ────────────────────────────
echo.
echo Closing any running instances of VHS Capture...
taskkill /f /im "VHS Capture.exe" >nul 2>&1
taskkill /f /im "VHS Capture v*.exe" >nul 2>&1

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
python -m pip install %PIP_USER% --upgrade ^
    maturin PyQt6 opencv-python numpy ^
    sounddevice imageio-ffmpeg ^
    pyinstaller >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo ERROR: pip install failed. See %LOGFILE%
    if not defined CI (
        pause
    )
    exit /b 1
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

:: Enable PyO3 ABI3 forward compatibility for Python 3.14+
set "PYO3_USE_ABI3_FORWARD_COMPATIBILITY=1"

:: Clean stale wheels so only the fresh Python 3.14/ABI3 wheel is installed
if exist "target\wheels" del /q "target\wheels\*.whl" >nul 2>&1

%MATURIN% build --release >> "%LOGFILE%" 2>&1
if errorlevel 1 (
    echo ERROR: Rust build failed. See %LOGFILE%
    echo.
    powershell -Command "Get-Content '%LOGFILE%' -Tail 30"
    cd "%~dp0"
    if not defined CI (
        pause
    )
    exit /b 1
)

:: Install the wheel
set "WHEEL="
for /f "delims=" %%f in ('dir /b /s /o:-d "target\wheels\*.whl" 2^>nul') do (
    if not defined WHEEL set "WHEEL=%%f"
)
if "%WHEEL%"=="" (
    echo ERROR: No wheel produced. See %LOGFILE%
    cd "%~dp0"
    if not defined CI (
        pause
    )
    exit /b 1
)
echo Installing: %WHEEL%
python -m pip install %PIP_USER% --force-reinstall "%WHEEL%" >> "%LOGFILE%" 2>&1
cd "%~dp0"
echo vhs_core installed.

:: ── Verify vhs_core loads ─────────────────────────────────────────
echo.
python -c "import vhs_core; assert hasattr(vhs_core, 'detect_blue'); print(' vhs_core OK')"
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

:: ── Build standalone exe ─────────────────────────────────────────
echo.
echo Ensuring icons and Windows version metadata are current...
python generate_icon.py >> "%LOGFILE%" 2>&1

echo.
echo Building standalone executable with Windows version info and icons...

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "VHS Capture" ^
    --add-data "README.md;." ^
    --add-data "vhs_capture.ico;." ^
    --icon "vhs_capture.ico" ^
    --version-file "file_version_info.txt" ^
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
    if not defined CI (
        pause
    )
    exit /b 1
)

:: Create version-named copy so Explorer immediately presents fresh icon without path cache collision
copy "%~dp0dist\VHS Capture.exe" "%~dp0dist\VHS Capture v0.4.1.exe" >nul 2>&1

:: Refresh Windows Explorer shell icon cache
python -c "import ctypes, os; ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None); p = os.path.abspath('dist/VHS Capture.exe'); ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, p, None) if os.path.exists(p) else None" >nul 2>&1

echo.
echo ============================================================
echo BUILD COMPLETE
echo.
echo Executables:
echo   - dist\VHS Capture.exe (with embedded icon & version resource)
echo   - dist\VHS Capture v0.4.1.exe
echo.
echo FFmpeg : bundled via imageio-ffmpeg (libsvtav1/libx264)
echo For GPU encoding (NVENC/QSV) also install:
echo    winget install ffmpeg
echo ============================================================
if not defined CI (
    pause
)