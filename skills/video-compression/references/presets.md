# Presets and decision rules

The skill has four presets. They follow the same preset logic as the Video Compressor iPhone and iPad app. This is a
re-implementation of the rules, not shared code, and the numbers that decide the target bitrate are calibrated by the skill itself.
The two parts are listed separately below so you know exactly what is mirrored and what is not.

| Preset | Keeps resolution | Intent |
| --- | --- | --- |
| `premium` | yes | Highest quality. Smaller files only when the source is clearly oversized. |
| `high` | yes | Visually close to the original at a much smaller size. |
| `balanced` (default) | longest edge capped at 1920 | Best everyday size and quality trade-off. |
| `max-saving` | longest edge capped at 1280 | Smallest files, noticeable softening on detailed footage. |

## Rules mirrored from the app

These rules decide whether a file is worth compressing and set the shape of the target.

| Rule | Value |
| --- | --- |
| Bitrate floor | A source at or below 1 Mbps is already small enough. Skipped as `below_bitrate_floor`. |
| Premium and efficient HEVC | `premium` skips HEVC sources at or below 5 Mbps (`premium_efficient_hevc`). |
| Minimum meaningful saving | A plan that would save less than 10% of the original is skipped. |
| Not-smaller gate | An output that is not smaller than the original is deleted and reported as `not_smaller`. The original is never touched. |
| Resolution cap | `premium` and `high` keep the resolution. `balanced` caps the longest edge at 1920, `max-saving` at 1280. Never upscales. |
| Density factor per preset | `premium` 1.75, `high` 1.40, `balanced` 1.14, `max-saving` 0.87 (multiplier on the base density). |
| Source bitrate ceiling | The target never exceeds 0.8 times the source video bitrate for `premium` and 0.6 times for the other presets. |
| Output share cap | `premium` must stay below 0.85 of the source size. |
| Rate control | Average bitrate encoding (not constant quality). |

## Skill-owned calibration (not from the app)

The app works on-device with Apple's encoder. A command-line skill has to estimate the same thing for any source codec
and any encoder, so these parts are the skill's own model:

| Part | Value | Why |
| --- | --- | --- |
| Base density | `BASE_BPP = 0.08` bits per pixel per frame, multiplied by the preset density factor | Chosen with VMAF checks on iPhone footage. Measured results per preset are in `benchmark.md`. |
| Frame rate scaling | `30 * (fps / 30) ** 0.75` effective frames per second | Higher frame rates need fewer extra bits per frame. |
| Codec efficiency | HEVC 1.0, AV1 1.2, VP9 1.0, H.264 0.7, other 0.6 | Used to judge whether a source is already efficient: `source bitrate * efficiency` at or below the target density means skip (`already_efficient`). |
| H.264 factor | 1.4 times the density for `--compat` output | H.264 needs more bits for similar quality. |
| Minimum saving rule | Applied to every preset | The app applies it to High only. A command-line run should not produce a file that barely changed. |

If you want to compare numbers, `python3 scripts/video_compress.py plan FILE --preset balanced --human` prints the
target bitrate, the estimate and the skip reason for each file.

## Target-size mode

`--target-mb N` or `--target discord|whatsapp|email|slack` fits the file under a size instead of using a preset.

- 1 MB is 1,000,000 bytes. The budget is multiplied by 0.95 to absorb container overhead and rate control drift.
- Audio gets 128, 96 or 64 kbps depending on how much total bitrate is available. AAC audio at or below that rate is copied.
- Video gets the remaining bitrate. When bits per pixel drop below 0.03 (times 1.4 for H.264) the resolution steps down a ladder
  (longest edge 2160, 1440, 1080, 720, 540, 480, 360) until the picture has enough bits.
- Software encoders (`libx265`, `libx264`) use two passes so the size is predictable. `hevc_videotoolbox` is a single pass. When any encoder
  overshoots the target, the encode is repeated with a lower bitrate, up to two more times.
- A target smaller than what the source can reasonably reach is reported as `target_too_small` instead of producing an unwatchable file.

## Skip reasons

| Reason | Meaning |
| --- | --- |
| `already_efficient` | The source bitrate is already at or below what the target would be. |
| `below_bitrate_floor` | The source is at or below 1 Mbps. |
| `premium_efficient_hevc` | `premium` and the source is HEVC at or below 5 Mbps. |
| `no_meaningful_savings`, `high_quality_no_meaningful_savings` | The estimate saves less than 10%. |
| `already_under_target` | The file already fits the requested size. |
| `target_too_small` | The requested size cannot hold a usable picture. |
| `live_photo_component` | The file is the video part of a Live Photo. Compress it in Photos, not as a file. |
| `format_not_preserved` | Cinematic or spatial video. A normal encode would drop the depth or stereo data. |
| `dolby_vision_profile5_needs_tonemap` | Dolby Vision profile 5 cannot be kept without tone mapping, so it is left alone. |
| `too_large_for_sandbox` | Hosted sandbox engine limits (120 seconds, 450 MB). Use `advise` or `--ignore-sandbox-limits`. |

`--force` encodes despite a skip reason and adds a `forced_despite_...` warning. Verification and the not-smaller gate still apply, and
an output that fails either is deleted.
