# Changelog

All notable changes to VHS Capture are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
