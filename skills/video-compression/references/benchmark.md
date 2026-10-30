# Benchmark (2026-10-30)

8 clips recorded on an iPhone 15 Pro (H.264 1080p about 30 fps, portrait and landscape, 205 s in total). Measured with `tools/benchmark.py` on arm64 Darwin 25.6.0. Sizes are file sizes, saving is `1 - output / original`, time is wall time per file including verification. VMAF is measured against the file that was compressed, frame-index aligned, measured on a centered 960x540 crop (540x960 for portrait) at 1080p scale, on the first 15 seconds of each clip, using every 5th frame, to keep the benchmark light (higher is better). Absolute VMAF values depend on this method and are not comparable with full-frame scores.

## Set 1: the 8 clips as recorded

| Mode | Encoded | Skipped as already efficient | Got bigger | Saving median (min to max) | Median time per file | VMAF median (min) |
| --- | --- | --- | --- | --- | --- | --- |
| `premium` | 8 of 8 | 0 | 0 | 41.1% (40.0% to 42.4%) | 1.6 s | 94.8 (90.7) |
| `high` | 8 of 8 | 0 | 0 | 52.5% (51.1% to 52.8%) | 1.5 s | 93.2 (89.7) |
| `balanced` | 8 of 8 | 0 | 0 | 60.7% (58.7% to 61.5%) | 1.5 s | 91.8 (87.0) |
| `max-saving` | 8 of 8 | 0 | 0 | 85.7% (84.8% to 86.0%) | 1.2 s | 80.6 (70.5) |
| `balanced-software` | 8 of 8 | 0 | 0 | 62.1% (55.9% to 65.4%) | 10.1 s | 95.3 (92.2) |
| `naive-libx264-crf23` | 8 of 8 | 0 | 0 | 67.2% (48.2% to 77.8%) | 12.5 s | 93.7 (91.1) |
| `naive-libx265-crf23` | 8 of 8 | 0 | 0 | 73.3% (48.1% to 84.1%) | 9.2 s | 94.1 (90.8) |

## Set 2: the 8 Balanced outputs from set 1 (already compressed once)

| Mode | Encoded | Skipped as already efficient | Got bigger | Saving median (min to max) | Median time per file | VMAF median (min) |
| --- | --- | --- | --- | --- | --- | --- |
| `premium` | 0 of 8 | 8 | 0 | n/a | n/a | n/a |
| `high` | 0 of 8 | 8 | 0 | n/a | n/a | n/a |
| `balanced` | 8 of 8 | 0 | 0 | 35.0% (29.3% to 36.3%) | 1.5 s | 93.6 (88.5) |
| `max-saving` | 8 of 8 | 0 | 0 | 63.4% (62.8% to 64.4%) | 1.0 s | 82.9 (78.2) |
| `balanced-software` | 8 of 8 | 0 | 0 | 37.7% (25.5% to 43.4%) | 9.0 s | 95.5 (92.3) |
| `naive-libx264-crf23` | 8 of 8 | 0 | 3 | 21.7% (-14.5% to 50.3%) | 13.4 s | 96.7 (91.5) |
| `naive-libx265-crf23` | 8 of 8 | 0 | 1 | 36.5% (-7.5% to 58.6%) | 12.6 s | 96.9 (91.4) |

## What the numbers show

- On clips as recorded, `balanced` saved a median of 60.7% and `premium` 41.1%. A naive `-crf 23` re-encode saved more (67.2% with libx264, 73.3% with libx265) at a similar or higher VMAF. The skill does not claim larger first-pass savings than a plain re-encode.
- `balanced-software` (libx265) scored a higher VMAF (95.3) than `balanced` with `hevc_videotoolbox` (91.8) at a similar size, and took 10.1 s per file instead of 1.5 s.
- `max-saving` trades quality for size (median VMAF 80.6).
- On footage that was already compressed once, `premium` and `high` skipped 8 and 8 of 8 files as already efficient. `balanced` and `max-saving` still re-encode them (a second lossy generation) because the sources are above their target bitrate.
- Naive `-crf 23` made 3 of 8 already-compressed files bigger with libx264 and 1 with libx265. The skill produced no output larger than its input (it deletes such an output and reports `not_smaller`).
- Clips are real iPhone recordings, so results for other content (screen recordings, animation, already-small web videos) will differ.

Skill modes use `hevc_videotoolbox` unless the name ends in `-software` (libx265). Naive modes are `ffmpeg -i in -c:v <codec> -crf 23 -c:a copy out.mp4` with default encoder settings. "Got bigger" counts outputs that are as large as or larger than the input. The skill never keeps such an output.
