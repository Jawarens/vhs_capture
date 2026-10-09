# 🔍 VHS Capture - Dependency Verification Script
# Run this to check if all components are properly installed

import sys
import importlib
import subprocess
import os
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def check_dependency(name, import_path=None, message=""):
    """Check if a Python dependency is installed."""
    try:
        if import_path:
            importlib.import_module(import_path)
        else:
            # Try common import names
            importlib.import_module(name)
        print(f"✅ {name}")
        return True
    except Exception as e:
        print(f"❌ {name} - {message or str(e)}")
        return False

def check_external_tool(name, cmd="--version"):
    """Check if an external tool (ffmpeg) is installed."""
    try:
        result = subprocess.run([name, cmd], capture_output=True, timeout=5, check=True)
        print(f"✅ {name} found")
        return True
    except Exception:
        print(f"❌ {name} not found or not in PATH")
        return False

def check_file(path):
    """Check if a file exists."""
    if Path(path).exists():
        print(f"✅ {path}")
        return True
    else:
        print(f"❌ {path} not found")
        return False

def main():
    print("=" * 60)
    print("VHS Capture - Dependency Verification")
    print("=" * 60)
    print()

    all_ok = True

    print("📦 Python Dependencies:")
    all_ok &= check_dependency("PyQt6", "PyQt6.QtWidgets")
    all_ok &= check_dependency("opencv-python", "cv2")
    all_ok &= check_dependency("numpy", "numpy")
    all_ok &= check_dependency("sounddevice", "sounddevice", "(optional - audio meter)")
    all_ok &= check_dependency("imageio-ffmpeg", "imageio_ffmpeg")

    print()
    print("🦀 Rust Extension:")
    try:
        import vhs_core
        if hasattr(vhs_core, "detect_blue"):
            print("✅ vhs_core imported successfully")
        else:
            print("⚠️  vhs_core extension not compiled (pure-Python fallback active)")
            print("   Run: ./build_windows.bat (Windows) or cargo build --release")
    except Exception:
        print("⚠️  vhs_core extension not found (pure-Python fallback active)")
        print("   Run: ./build_windows.bat (Windows) or cargo build --release")

    print()
    print("🎯 Files & Configuration:")
    all_ok &= check_file("vhs_capture.py")
    all_ok &= check_file("vhs_capture.ico")
    all_ok &= check_file("requirements.txt")
    all_ok &= check_file("SETUP.md")
    all_ok &= check_file("VHS Capture.spec")

    print()
    print("🔧 Build Dependencies:")
    all_ok &= check_dependency("setuptools", "setuptools")
    all_ok &= check_dependency("wheel", "wheel")

    print()
    print("🛠️  Development Dependencies (Optional):")
    check_dependency("mypy", "mypy", "(optional - type checking)")
    check_dependency("ruff", "ruff", "(optional - linting)")

    print()
    print("💾 System Tools:")
    # Check for FFmpeg
    has_ffmpeg_system = check_external_tool("ffmpeg", "-version")
    if not has_ffmpeg_system:
        print("   ⚠️  System FFmpeg not required (uses bundled imageio-ffmpeg)")
        print("   For GPU encoding: run 'winget install ffmpeg' (Windows)")

    print()
    print("🐍 Python Environment:")
    print(f"   Python: {sys.version}")
    print(f"   Platform: {sys.platform}")

    print()
    print("=" * 60)
    if all_ok:
        print("🎉 All required components OK! App should run.")
    else:
        print("⚠️  Some components missing. See suggestions above.")
    print("=" * 60)

    return all_ok

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
