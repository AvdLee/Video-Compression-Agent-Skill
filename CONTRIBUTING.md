# Contributing

Contributions should make video compression safer, more predictable or better explained.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall skills/video-compression/scripts tests tools
for f in plugin.json .claude-plugin/plugin.json .claude-plugin/marketplace.json .cursor-plugin/plugin.json .codex-plugin/plugin.json package.json; do python3 -m json.tool "$f" >/dev/null; done
```

The tests call the script the way an agent would and compare its claims with independent `ffprobe` output on clips generated into a
temporary folder. They need `ffmpeg` and `ffprobe`. Tests that need macOS `avconvert` skip on other systems. The network test only runs
with `VC_TEST_NETWORK=1`.

## Requirements

- Use Python's standard library.
- Never touch originals. Never add wildcard deletion.
- Add a deterministic test for every new skip reason or safety guard.
- Back platform limits with an official page and a date.
- Measure before you claim: use `tools/benchmark.py` and record the clip count and date.

Open a pull request describing the behavior change, the evidence and how you verified it.

## Benchmark

```bash
python3 tools/benchmark.py CLIPS_DIR --stage source --work-dir /tmp/vc-bench
python3 tools/benchmark.py CLIPS_DIR --stage reencoded --work-dir /tmp/vc-bench
python3 tools/benchmark.py CLIPS_DIR --stage render --work-dir /tmp/vc-bench
```

Each stage runs one encode at a time and cleans up its child processes. Delete the work folder afterwards.
