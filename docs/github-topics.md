# GitHub topics and listing notes

Repository topics to set when the repository is published:

`agent-skills`, `claude-code`, `codex`, `cursor`, `ffmpeg`, `video-compression`, `hevc`, `ios`

Suggested repository description: "Agent Skill that compresses videos with ffmpeg or macOS avconvert, skips files that are already efficient, verifies every result and never touches the original."

Notes:

- The README carries no version numbers, so it stays correct between releases.
- Listing metadata (display name, subtitle, default prompts, icons) lives in `.codex-plugin/plugin.json`. The root `plugin.json` has a
  closed schema and carries only the portable fields. Display name is limited to 30 characters and the subtitle to 30 characters.
- `requires_local_executor` is intentionally not set anywhere.
