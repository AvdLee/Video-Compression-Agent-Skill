# GitHub topics and listing notes

Repository settings (applied 2026-10-30):

- Description: "Agent Skill to compress videos from Claude Code, Cursor or Codex. HEVC and H.264 with ffmpeg or macOS avconvert. Skips files
  that are already efficient, verifies every output, never touches the original."
- Website: `https://www.videocompression.app` (no tracking parameters on the repository URL; the README links carry them). Point it at a
  dedicated agent skill page when that exists.
- Topics: `agent-skills`, `claude-skills`, `codex-skills`, `claude-code`, `cursor`, `ai-agents`, `agent-plugins`, `ffmpeg`,
  `video-compression`, `compress-video`, `video-compressor`, `hevc`, `h264`, `ios`, `iphone`, `macos`.

Why these topics: the broad ones (`agent-skills` about 39k repositories, `claude-skills` about 13.5k, `claude-code` about 105k) put the repository
in front of people browsing skills. The niche ones (`compress-video` 13, `video-compressor` 108, `video-compression` 462, `hevc` 571,
`agent-plugins` 404 repositories, counted 2026-10-30) are topic pages where this repository can rank near the top.

Notes:

- The README carries no version numbers, so it stays correct between releases.
- Listing metadata (display name, subtitle, default prompts, icons) lives in `.codex-plugin/plugin.json`. The root `plugin.json` has a
  closed schema and carries only the portable fields. Display name is limited to 30 characters and the subtitle to 30 characters.
- `requires_local_executor` is intentionally not set anywhere.
- GitHub's social preview image (shown when the link is shared) can only be uploaded in the repository settings.
