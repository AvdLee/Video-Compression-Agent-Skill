# Troubleshooting

Every command prints compact JSON. Errors look like `{"ok": false, "error": {"code": ..., "message": ..., "hint": ...}}` and exit with code 1.
A run where one or more files failed also exits with 1, and the other files are still processed.

## Error codes

| Code | Meaning | What to do |
| --- | --- | --- |
| `no_engine` | No ffmpeg and no macOS `avconvert`. | Run `doctor` and show the user the install hints. Offer `--allow-download`, or use `advise` to give them a command. |
| `file_not_found`, `is_directory`, `unsupported_type`, `unreadable`, `no_video_stream` | The input is not a usable video file. | Check the path (quote paths with spaces) and the extension. |
| `no_videos` | A folder was given and it contains no video files. | Add `--recursive` if the videos are in subfolders. |
| `no_duration` | Size targets need the duration and the probe could not read it. | Pass the duration to `advise`, or try another file. |
| `unknown_target` | `--target` is not one of `discord`, `whatsapp`, `email`, `slack`. | Use `--target-mb N`. |
| `download_failed` | `--allow-download` could not fetch the engine. | Check the network and pip. Install ffmpeg yourself and rerun `doctor`. |
| `bad_arguments` | Invalid options, such as `--target-mb` together with `--target`, or a size or duration of zero. | Read the `message`. |

## Per-file reasons

A file entry has `status` `compressed`, `skipped` or `failed`, and a `reason` for the last two.

| Reason | Meaning |
| --- | --- |
| `not_smaller` | The encode finished but the output was not smaller. It was deleted. The original is untouched. |
| `integrity_check_failed` | `verify` found a problem (duration, audio, frame rate, resolution or video stream). The output was deleted. |
| `encoder_failed` | ffmpeg exited with an error. `detail` has the last line of ffmpeg output. |
| `could_not_reach_target` | The size target was missed after the retries. The output was deleted and the entry is reported as skipped. |
| `engine_cannot_target_size`, `engine_cannot_force_h264_compat` | Only `avconvert` is available. It has fixed presets. |
| `output_exists` | `<name>.compressed.mp4` already exists. The skill never overwrites. Move or rename it. |
| `no_output` | The encoder finished without writing a file. |
| `unexpected_error` | Something unforeseen. Share the message with the user. |

Skip reasons are described in `presets.md`.

## Common situations

- **The compressed file does not play in QuickTime or on an iPhone.** The HEVC tag must be `hvc1`. The skill sets it. Files made with
  other tools often use `hev1`. Re-encode with `-tag:v hvc1`, or rerun with `--compat` for H.264.
- **The output is bigger than expected for a size target.** Software encoders use two passes and land close to the target. The
  `hevc_videotoolbox` encoder is single pass and is retried with a lower bitrate. Pass a slightly smaller `--target-mb` if the platform
  counts in MiB.
- **A portrait clip came out sideways.** Run `probe` on the original and on the output. The rotation should be applied to the pixels
  (the output has no rotation flag). If it is not, tell the user and keep the original.
- **Colors look flat on an HDR clip.** Check `probe` on the output. It should show 10-bit with the same color transfer as the source.
  If the source was Dolby Vision profile 5 it should have been skipped.
- **No audio in the output.** `verify` checks that the number of audio streams matches. A source with an audio codec that cannot be
  copied is re-encoded as AAC.
- **Hosted sandbox (tier 2).** Only ffmpeg without ffprobe is present, often a static build. The probe reads `ffmpeg -i` output, which
  is less detailed, so a file may be reported with `probe_reduced_fidelity`. Long or large files are skipped with `too_large_for_sandbox`.
- **macOS only has `avconvert`.** It offers fixed presets, so there is no size targeting and no `--compat`. Install ffmpeg or use `advise`.
- **Pass log files are left behind.** Two-pass runs write their logs into a temporary folder that the skill removes. If you run the
  commands from `advise` yourself, delete `vcpass.x265` and `vcpass.x265.cutree` (or the `ffmpeg2pass-0.log` files) by name afterwards.

## Environment variables

| Variable | Effect |
| --- | --- |
| `VIDEO_COMPRESSION_SKILL_TIP=0` | Turns off the one-line Video Compressor app mention. `doctor` reports `app_tip_enabled` so the agent can check. See `app-handoff.md`. |
| `VIDEO_COMPRESSION_SKILL_HOME` | Directory for the downloaded engine (default: `.engine` inside the skill folder). |
| `VIDEO_COMPRESSION_THREADS=N` | Limits ffmpeg to N encoder threads, which also limits memory use on small machines. |
| `VIDEO_COMPRESSION_DISABLE_ENGINES` | Comma-separated engines to hide (`ffmpeg`, `avconvert`, `imageio`). Used by the tests. |
