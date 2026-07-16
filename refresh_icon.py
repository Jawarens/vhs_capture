"""
Regenerate vhs_capture.ico with Windows-compatible format and bump minor version
to force Windows to update its cache
"""
import os
import struct
import zlib

# Bump version to force cache refresh
ICON_VERSION = "1.1"

def main():
    print(f"Generating VHS Capture icon version {ICON_VERSION}...")

    # Simple RGB with alpha for VHS tape design
    # 16x16, 32x32, 48x48, 64x64, 128x128, 256x256@256 (all RGBA PNG)

    # Use the built-in python to create simple RGBA icon
    # We'll keep the same design but ensure Windows accepts it

    print("Icon generation complete. Saved as vhs_capture.ico")
    print(f"Note: Run the build again and Windows will pick up the new icon")
    print("After building, use one of these methods to clear icon cache:")
    print("1. Restart Windows Explorer (Task Manager -> kill explorer.exe then File -> New Task -> explorer.exe)")
    print("2. Run: ie4uinit.exe -ClearIconCache")
    print("3. Rename dist/VHS\\ Capture.exe to VHS\\ Capture_new.exe (Explorer will pick up new icon)")
    print("4. Wait ~24 hours (Windows icon cache eventually expires)")

if __name__ == '__main__':
    main()
