# Agent guidance

This repository contains a safety-sensitive Agent Skill that writes files next to the user's videos.

- Never modify, move or delete an original file. Outputs are written as `<name>.compressed.mp4` and never overwrite an existing file.
- Delete only files the script created itself, by exact name (the `.partial.mp4` files). No wildcard deletion, no `sudo`.
- Keep the script on the Python standard library. No telemetry. The only network use is the explicit `--allow-download`.
- The script must not install software by itself. `doctor` prints install hints only.
- Every new skip reason, preset rule or platform limit needs a test in `tests/test_video_compress.py` and an entry in the matching
  reference file. Platform limits need an official source and a retrieval date in `references/platform-limits.md`.
- Every number in public copy (README, `references/benchmark.md`) must come from `tools/benchmark.py` output with the clip count and date.
- Public text uses plain, factual language. No pricing, no claims about other tools that are not verified.
- Run `python3 -m unittest discover -s tests -v` and the validator before completing changes.
- Never commit video files.
