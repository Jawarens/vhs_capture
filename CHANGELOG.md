# Changelog

All notable changes to VHS Capture are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.1] — 2026-10-12

### Changed
- GitHub Actions Windows build & release workflow; `build_windows.bat` now runs non-interactively under CI.

## [0.4.0] — 2026-09-27

### Added
- **Precision A/V synchronization & drop-frame protection**: Wall-clock timestamp matching dynamically detects when capture cards or tape creases drop frames, inserting duplicate frames to maintain strict 1:1 video-to-audio sync across multi-hour captures.
- **Audio drift correction**: Added `-af aresample=async=1000` to audio muxing pipeline to resample minor clock drift between USB audio clocks and video feeds.
- **Native VHS PAL & NTSC standards**: Added native resolution profiles for PAL (720×576 @ 25fps) and NTSC (720×480 @ 29.97fps) alongside 4:3 DAR metadata.
- **Motion-adaptive deinterlacing**: Integrated `bwdif` (standard and 2x double-rate smooth motion) and `yadif` filters to eliminate interlacing comb artifacts on motion.
- **Head-switching noise masking**: Optional filter masking the bottom 12 noisy scanlines with a clean solid black bar, eliminating flickering artifacts and saving bitrate.
- **Multi-signal detection**: Added black/blank screen detection and analog static/snow RF noise detection in addition to VHS blue screen.
- **Unattended End-of-Tape auto-stop**: Automatically stops and finalizes recording when blank screen (blue/black/static) and audio silence persist for a configurable duration (default 30s).
- **DirectShow hardware settings dialog**: New "⚙️ Device settings" button triggering the capture card's native DirectShow property sheet on Windows to switch between Composite and S-Video inputs and tune hardware brightness/contrast.
- **Enhanced audio controls**: Added mono-to-stereo channel duplication (Left to both / Right to both), 50Hz/60Hz mains ground-loop hum filter, and EBU R128 loudness normalization (`loudnorm`).
- **Lossless Archival Master (FFV1)**: Added FFV1 codec in an MKV container with lossless FLAC audio for preservation-grade captures.
- **Real-time disk space guard**: Live free disk space and estimated recording hours monitoring, with automatic emergency stop if free space drops below 1.5 GB.
- **Frame snapshot capture**: Button and `Ctrl+S` shortcut to save full-resolution lossless PNG frame stills.
- **CMX 3600 EDL & MKV chapter export**: Automatic export of industry-standard CMX 3600 Edit Decision Lists and FFmpeg chapter metadata files for video editors and media servers.

### Fixed
- **Removed obsolete patch script**: Cleaned up leftover `fix_audio.py`.
- **NumPy saturation division warning**: Replaced unsafe array division in color analysis with safe `np.divide(..., where=...)`.

## [0.3.1] — 2026-09-26

### Added
- **Modern vector icon suite**: High-DPI anti-aliased icon generator (`generate_icon.py`) producing multi-resolution `.ico` (256, 128, 64, 48, 32, 16 px) and 1024x1024 master preview (`vhs_camcorder_icon.png`), combining obsidian camcorder body, optical coated lens, smoked cassette window, tape spools, and classic VHS tricolor racing stripes.
- **In-app UI iconography**: Camcorder action button glyphs (`📹 Open camera` / `📹 Change camera`, `⏺ Start recording` / `⏹ Stop recording`, `✂ Split now`), modal window icons, and tactile button micro-interactions (responsive hover and pressed states).
- **Header icon badge**: Integrated 38×38 application branding mark in the main window header.

### Fixed
- **File locking race condition on audio/video muxing**: Replaced in-place temporary file writes in `stop_and_mux` with transient `.mux_tmp*` files, explicit ffmpeg subprocess cleanup, Windows file lock retry loop, and stale temp cleanup on startup.
- **Frame loss during auto-splits**: Implemented `_flush_queue_to` in `_WriterThread` ensuring frames queued during split boundaries are cleanly flushed into the new segment instead of being dropped.
- **Windows console crash in verification tools**: Fixed Windows CP1252 stdout encoding crashes in `verify.py` and `test_icons.py` using `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`.

### Optimized
- **Native blue detection zero-copy buffer**: Optimized `vhs_core` to inspect frame contiguous memory directly without allocating a 6.2 MB duplicate buffer on every single video frame, reducing blue detection overhead from 6.2 MB to 10.8 KB per frame.
- **Direct BGR888 preview rendering**: Updated preview pipeline to use `QImage.Format.Format_BGR888`, avoiding expensive per-frame OpenCV `cvtColor(BGR2RGB)` copies in the UI thread.

## [0.3.0] — 2026-07-16

### Added
- **Keyboard shortcuts**: `Space` toggles recording, `Enter` splits clip, `Ctrl+O` opens camera picker. Hint shown in event log on startup.
- **Clips list metadata**: saved clips now display file size (MB) after mux completes, with live refresh on audio mux finish.
- **Event log clear button**: small "Clear" button in the top-right of the Event Log section.
- **Filename preview monospace styling**: output filename preview now uses a monospace font for easier reading.

### Changed
- **Camera enumeration**: now probes cameras in parallel via `ThreadPoolExecutor` (was sequential), reducing scan time from ~10s to ~2-3s.
- **Preview resize quality**: upgraded from `INTER_NEAREST` to `INTER_LINEAR` for visibly sharper preview image.
- **Audio meter bar**: increased from 6px to 14px height for better visibility.

## [0.2.0] — 2026-07-15

### Added
- Tape timer with preset buttons (30m, 45m, 60m, 90m, 120m) and custom duration.
- Auto-stop recording when tape length is reached.
- System FFmpeg detection for GPU encoding (NVENC, QuickSync).
- Encoder probing with parallel FFmpeg process checks.
- Custom title and part prefix for output filenames.
- Clips list showing all saved segments.
- Dark/light theme following Windows system preference.
- Comprehensive FFmpeg hardware encoder support (AV1/HEVC/H.264 via NVENC and QSV).
- Rust extension (`vhs_core`) with optimized blue detection and lock-free frame queue.

### Changed
- Improved blue screen detection with hysteresis to avoid false positives.
- Tape timer remaining time display.
- Stats display showing recording time, clips saved, and blue screen splits.
- Event log with color-coded entries.

## [0.1.0] — Initial Release

### Added
- Webcam capture with configurable resolution and FPS.
- Blue screen detection and auto-clip splitting.
- Audio capture and muxing into video files.
- Multiple encoder options (SVT-AV1, libaom, x265, x264).
- Settings persistence across sessions.
- Custom VHS-themed application icon.
