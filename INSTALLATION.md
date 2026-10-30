# All Installation Options

The [README](README.md) covers skills.sh, Claude Code, Cursor and Codex. This file lists every other way to install the skill.

## pi package manager

```bash
pi install https://github.com/AvdLee/Video-Compression-Agent-Skill
```

The root `package.json` points pi at `skills/video-compression`.

## Gemini CLI

```bash
gemini skills install https://github.com/AvdLee/Video-Compression-Agent-Skill
```

Add `--scope workspace` to share the skill with your team through version control. Check the result with `gemini skills list`.

## Manual install

1. Clone this repository.
2. Copy or symlink the `skills/video-compression/` folder into the skills folder your tool reads (for example `~/.claude/skills/`,
   `~/.agents/skills/` or `.cursor/skills/` in a project).
3. Ask your agent to use the "video-compression" skill.

The folder is self-contained. The script is `skills/video-compression/scripts/video_compress.py` and needs only Python 3.

## Checking the install

Run this from the skill folder. It never installs anything and prints which tier your machine supports:

```bash
python3 scripts/video_compress.py doctor --human
```
