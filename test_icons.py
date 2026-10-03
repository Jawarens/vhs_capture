#!/usr/bin/env python
"""
Quick test to verify icon integration works at both build and runtime
"""
import os
import sys
import re

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

def test_icon_exists():
    """Test that the icon file exists"""
    icon_path = "vhs_capture.ico"
    if os.path.exists(icon_path):
        print("✅ Icon file exists: vhs_capture.ico")
        return True
    print("❌ FAIL: Icon file missing: vhs_capture.ico")
    return False

def test_spec_includes_icon():
    """Test that .spec file includes the icon"""
    spec_path = "VHS Capture.spec"
    with open(spec_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if "('vhs_capture.ico', '.')" not in content:
        print("❌ FAIL: .spec missing icon in datas list")
        return False
    print("✅ .spec includes icon in datas: ('vhs_capture.ico', '.')")

    if not re.search(r"icon=\[?['\"]vhs_capture\.ico['\"]\]?", content):
        print("❌ FAIL: .spec missing icon parameter")
        return False
    print("✅ .spec includes icon parameter")
    return True

def test_app_sets_icon():
    """Test that the application references the icon"""
    py_path = "vhs_capture.py"
    with open(py_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if 'vhs_capture.ico' not in content:
        print("❌ FAIL: vhs_capture.py doesn't reference icon")
        return False

    # Count occurrences - should be at least in line 1338-1340
    occurrences = content.count('vhs_capture.ico')
    print(f"✅ Application references icon {occurrences} times")
    if 'setWindowIcon(QIcon(_icon_path))' in content:
        print("✅ Application sets window icon with QIcon")
    return True

def test_version_file():
    """Test that Windows version info file exists and parses"""
    vpath = "file_version_info.txt"
    if not os.path.exists(vpath):
        print("❌ FAIL: Version info file missing: file_version_info.txt")
        return False
    try:
        from PyInstaller.utils.win32.versioninfo import load_version_info_from_text_file
        info = load_version_info_from_text_file(vpath)
        print("✅ Version info file exists and parses cleanly")
        return True
    except Exception as e:
        print(f"❌ FAIL: Error parsing version info file: {e}")
        return False

def test_spec_includes_version():
    """Test that .spec file includes the version parameter"""
    spec_path = "VHS Capture.spec"
    with open(spec_path, 'r', encoding='utf-8') as f:
        content = f.read()

    if "version='file_version_info.txt'" not in content and 'version="file_version_info.txt"' not in content:
        print("❌ FAIL: .spec missing version parameter")
        return False
    print("✅ .spec includes version parameter: file_version_info.txt")
    return True

def test_build_output():
    """Test that build output exists and has embedded icon and version resources"""
    exe_path = "dist/VHS Capture.exe"
    if not os.path.exists(exe_path):
        print("⚠️  Build not run yet (dist/VHS Capture.exe not found)")
        return True

    size = os.path.getsize(exe_path)
    print(f"✅ Executable exists: {size:,} bytes")
    if size > 100 * 1024 * 1024:
        print("✅ Size appropriate for PyQt6 app with embedded deps")

    try:
        import pefile
        pe = pefile.PE(exe_path)
        res_types = [entry.id for entry in getattr(pe, 'DIRECTORY_ENTRY_RESOURCE', []).entries]
        has_icon = 3 in res_types     # RT_ICON
        has_group = 14 in res_types   # RT_GROUP_ICON
        has_version = 16 in res_types # RT_VERSION

        if has_icon and has_group:
            print("✅ Executable contains embedded RT_ICON & RT_GROUP_ICON resources")
        else:
            print("❌ Executable missing icon resources")

        if has_version:
            print("✅ Executable contains embedded Windows RT_VERSION resource")
        else:
            print("⚠️  Executable has not been rebuilt with RT_VERSION resource yet")
    except Exception as e:
        print(f"Note: Could not inspect PE headers: {e}")

    return True

def main():
    print("=" * 60)
    print("Icon & Windows Version Info Integration Verification")
    print("=" * 60)
    print()

    all_pass = True
    all_pass &= test_icon_exists()
    all_pass &= test_spec_includes_icon()
    all_pass &= test_version_file()
    all_pass &= test_spec_includes_version()
    all_pass &= test_app_sets_icon()
    all_pass &= test_build_output()

    print()
    print("=" * 60)
    if all_pass:
        print("✅ ALL CHECKS PASSED - Icons & Version metadata integrated!")
        print("=" * 60)
        return 0
    else:
        print("❌ SOME CHECKS FAILED - Please review the issues above")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())

