# Apple video notes

What matters when the source or the destination is an iPhone, iPad, Mac or Apple TV.

## HEVC must be tagged `hvc1`

QuickTime, Photos, iMessage and Safari only play HEVC in MP4 or MOV when the sample entry is `hvc1`. ffmpeg tags libx265 output
as `hev1` by default, which many Apple players refuse. The skill always passes `-tag:v hvc1`. If you run your own command, add it.

## Encoders

- macOS: `hevc_videotoolbox` uses the hardware encoder and is the default. It is fast and good, but it is an average-bitrate
  encoder without a CRF mode.
- Linux and Windows: `libx265` (software). Slower, and size is easier to control with two passes.
- `--compat` writes H.264 for players that do not decode HEVC (some TVs, older browsers, some editors). H.264 files are larger for
  the same quality.

## HDR, HLG and Dolby Vision

iPhones record HDR as 10-bit HEVC with HLG, and many clips carry Dolby Vision profile 8.4 metadata on top of the HLG base layer.

- The skill keeps 10-bit output (`main10`, `yuv420p10le` or `p010le`) and copies `color_primaries`, `color_trc` and `colorspace`
  so the clip still looks like HDR.
- Dolby Vision profile 8 files are encoded from the HLG base layer. The Dolby Vision metadata is dropped and the output is plain HLG
  HDR video. A warning (`dolby_vision_metadata_dropped_base_layer_kept`) is reported.
- Dolby Vision profile 5 has no compatible base layer. Without tone mapping the picture would look wrong, so the file is skipped
  (`dolby_vision_profile5_needs_tonemap`).
- `--hdr skip` leaves every HDR file alone.
- HDR cannot be kept with `--compat`. Those files are skipped (`hdr_cannot_be_kept_with_h264_compat`).

## Live Photos, Cinematic and spatial video

These formats store more than a plain video track.

- Live Photo videos carry a content identifier that pairs them with a still photo. A compressed copy is no longer paired with the photo,
  so the skill skips such files (`live_photo_component`). Compress the library item on the device instead.
- Cinematic mode clips carry depth and focus data. A normal encode drops it, so the skill skips them (`format_not_preserved`).
- Spatial video (stereo MV-HEVC) loses the second eye in a normal encode. Skipped as `format_not_preserved`.

These checks look at container tags and stream properties. They are heuristics and have not been verified against every iOS
release. If a file is skipped by mistake, `--force` overrides the check.

## Rotation

iPhone portrait clips are stored landscape with a rotation flag. The skill applies the rotation to the pixels while encoding so the
result has the correct orientation everywhere. `probe` reports `display_width` and `display_height` after rotation.

## Variable frame rate

Some recordings have a variable frame rate (`probe` reports it as `vfr`). The skill keeps the
source timestamps. It reports `variable_frame_rate_source` as a warning because some editors expect a constant rate.

## Metadata

Creation date, location and other container metadata are copied (`-map_metadata 0`). Data streams and subtitle streams are not copied
and the skill warns when a source has them (`data_and_subtitle_streams_not_copied`).
