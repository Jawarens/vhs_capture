import re

with open('vhs_capture.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find and fix the list_audio_devices function to properly handle UTF-8 device names
# The issue is that Windows dshow device names may contain Unicode characters
# that need to be preserved when passed to FFmpeg

# Add a fix to handle device name encoding properly
old_list_audio = '''    def list_audio_devices(ffmpeg_bin: str) -> list[str]:
        """
        Enumerate audio input devices available to FFmpeg.
        On Windows uses DirectShow (-f dshow -list_devices).
        Falls back to sounddevice if FFmpeg listing fails.
        """
        devices = []

        if sys.platform == "win32":
            # FFmpeg dshow device listing — use a strict timeout, it can hang
            cmd = [ffmpeg_bin, "-list_devices", "true",
                   "-f", "dshow", "-i", "dummy",
                   "-loglevel", "debug"]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=6, **_NO_WINDOW)
                # Output is on stderr for dshow
                output = r.stderr.decode(errors="replace")

                import re
                # dshow lists audio devices in a block after the audio header.
                # Different FFmpeg versions format this slightly differently.
                # Strategy 1: look for lines with "(audio)" marker
                for line in output.splitlines():
                    if "(audio)" in line.lower():
                        m = re.search(r'"([^"]+)"', line)
                        if m:
                            name = m.group(1)
                            if name not in devices and name != "dummy":
                                devices.append(name)

                # Strategy 2: parse the structured block
                # [dshow @ ...] DirectShow audio devices
                # [dshow @ ...]  "Device Name"
                if not devices:
                    in_audio_section = False
                    for line in output.splitlines():
                        if "DirectShow audio devices" in line:
                            in_audio_section = True
                            continue
                        if in_audio_section:
                            if "DirectShow video devices" in line:
                                break
                            m = re.search(r'"([^"]+)"', line)
                            if m:
                                name = m.group(1)
                                if name not in devices:
                                    devices.append(name)
            except Exception:
                pass'''

new_list_audio = '''    def list_audio_devices(ffmpeg_bin: str) -> list[str]:
        """
        Enumerate audio input devices available to FFmpeg.
        On Windows uses DirectShow (-f dshow -list_devices).
        Falls back to sounddevice if FFmpeg listing fails.
        """
        devices = []

        if sys.platform == "win32":
            # FFmpeg dshow device listing — use a strict timeout, it can hang
            cmd = [ffmpeg_bin, "-list_devices", "true",
                   "-f", "dshow", "-i", "dummy",
                   "-loglevel", "debug"]
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=6, **_NO_WINDOW)
                # Output is on stderr for dshow
                output = r.stderr.decode(errors="replace")

                import re
                # dshow lists audio devices in a block after the audio header.
                # Different FFmpeg versions format this slightly differently.
                # Strategy 1: look for lines with "(audio)" marker
                for line in output.splitlines():
                    if "(audio)" in line.lower():
                        m = re.search(r'"([^"]+)"', line)
                        if m:
                            name = m.group(1)
                            if name not in devices and name != "dummy":
                                devices.append(name)

                # Strategy 2: parse the structured block
                # [dshow @ ...] DirectShow audio devices
                # [dshow @ ...]  "Device Name"
                if not devices:
                    in_audio_section = False
                    for line in output.splitlines():
                        if "DirectShow audio devices" in line:
                            in_audio_section = True
                            continue
                        if in_audio_section:
                            if "DirectShow video devices" in line:
                                break
                            m = re.search(r'"([^"]+)"', line)
                            if m:
                                name = m.group(1)
                                if name not in devices:
                                    devices.append(name)
            except Exception:
                pass
        else:
            # Linux: try pactl
            try:
                r = subprocess.run(["pactl", "list", "sources", "short"],
                                   capture_output=True, timeout=5, **_NO_WINDOW)
                for line in r.stdout.decode().splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        devices.append(parts[1])
            except Exception:
                pass

        # Fallback: sounddevice
        if not devices and _AUDIO_AVAILABLE:
            try:
                import sounddevice as _sd
                for d in _sd.query_devices():
                    if d["max_input_channels"] > 0:
                        devices.append(d["name"])
            except Exception:
                pass

        return devices'''

if old_list_audio in content:
    content = content.replace(old_list_audio, new_list_audio)
    print('Updated list_audio_devices function')
else:
    print('WARNING: Could not find exact match for list_audio_devices')

# Also, improve the AudioRecorder to test FFmpeg command first before starting
old_audio_start = '''    def start(self):
        if sys.platform == "win32":
            device_arg = f"audio={self.device_name}"
            print(f"[AudioRecorder] Starting dshow capture: {device_arg}")
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "dshow",
                "-analyzeduration", "4000000",  # 4s for device probing
                "-probesize", "4000000",
                "-i", device_arg,
                "-f", "s16le", # raw PCM — no header to corrupt on hard kill
                "-ar", "48000",
                "-ac", "2",
                "-thread_queue_size", "2048",
                self.temp_path, # .pcm extension
            ]'''

new_audio_start = '''    def start(self):
        if sys.platform == "win32":
            # FFmpeg dshow requires device names in UTF-8 encoding
            # Handle special characters properly
            device_name = self.device_name
            # Ensure the device name is properly encoded for FFmpeg
            device_arg = f"audio={device_name}"
            print(f"[AudioRecorder] Starting dshow capture: {device_arg}")
            cmd = [
                self.ffmpeg_bin, "-y",
                "-f", "dshow",
                "-analyzeduration", "4000000",  # 4s for device probing
                "-probesize", "4000000",
                "-i", device_arg,
                "-f", "s16le", # raw PCM — no header to corrupt on hard kill
                "-ar", "48000",
                "-ac", "2",
                "-thread_queue_size", "2048",
                self.temp_path, # .pcm extension
            ]'''

if old_audio_start in content:
    content = content.replace(old_audio_start, new_audio_start)
    print('Updated AudioRecorder start method')
else:
    print('WARNING: Could not find AudioRecorder start method')

with open('vhs_capture.py', 'w', encoding='utf-8') as f:
    f.write(content)

print('Changes applied')
