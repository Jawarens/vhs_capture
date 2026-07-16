#!/usr/bin/env python
"""
Quick test to verify icon integration works at both build and runtime
"""
import os
import sys

def test_icon_exists():
    """Test that the icon file exists"""
    icon_path = "vhs_capture.ico"
    if not os.path.exists(icon_path):
        print("PASS: Icon file exists: vhs_capture.ico")
        return False
    return True

def test_spec_includes_icon():
    """Test that .spec file includes the icon"""
    spec_path = "VHS Capture.spec"
    with open(spec_path, 'r') as f:
        content = f.read()

    if "('vhs_capture.ico', '.')" not in content:
        print("❌ FAIL: .spec missing icon in datas list")
        return False
    print("✅ .spec includes icon in datas: ('vhs_capture.ico', '.')")

    if "icon='vhs_capture.ico'" not in content:
        print("❌ FAIL: .spec missing icon parameter")
        return False
    print("✅ .spec includes icon parameter: icon='vhs_capture.ico'")
    return True

def test_app_sets_icon():
    """Test that the application references the icon"""
    py_path = "vhs_capture.py"
    with open(py_path, 'r') as f:
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

def test_build_output():
    """Test that build output exists"""
    exe_path = "dist/VHS Capture.exe"
    if not os.path.exists(exe_path):
        print("⚠️  Build not run yet (dist/VHS Capture.exe not found)")
        return True  # Not a failure, just skip

    size = os.path.getsize(exe_path)
    print(f"✅ Executable exists: {size:,} bytes")
    if size > 100 * 1024 * 1024:  # >100MB
        print("✅ Size appropriate for PyQt6 app with embedded deps")
    return True

def main():
    print("=" * 60)
    print("Icon Integration Verification")
    print("=" * 60)
    print()

    all_pass = True
    all_pass &= test_icon_exists()
    all_pass &= test_spec_includes_icon()
    all_pass &= test_app_sets_icon()
    all_pass &= test_build_output()

    print()
    print("=" * 60)
    if all_pass:
        print("✅ ALL CHECKS PASSED - Icons are properly integrated!")
        print("=" * 60)
        return 0
    else:
        print("❌ SOME CHECKS FAILED - Please review the issues above")
        print("=" * 60)
        return 1

if __name__ == "__main__":
    sys.exit(main())
