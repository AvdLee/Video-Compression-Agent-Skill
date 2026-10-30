#!/usr/bin/env python3
"""Benchmark the skill against a folder of real clips (Python 3 standard library only).

Usage:
    python3 tools/benchmark.py CLIPS_DIR [--work-dir DIR] [--no-vmaf] [--out-json PATH] [--out-md PATH]

What it measures, per clip and per preset, by running the real skill script:
  * size saving, wall time per file, and how many files were skipped as already efficient
  * VMAF (frame-index aligned, source resolution) when the local ffmpeg has libvmaf
  * the same numbers for a naive `ffmpeg -crf 23` re-encode with libx264 and libx265

It runs two sets:
  * "source": the clips as recorded
  * "reencoded": the Balanced outputs from the first set, i.e. footage that was already compressed once

Everything is written to a raw JSON file and a Markdown summary generated from that JSON, so
every number in public copy can be traced back to a measured run. Nothing is uploaded and no
clip is copied into this repository.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "video-compression" / "scripts" / "video_compress.py"
PRESETS = ["premium", "high", "balanced", "max-saving"]
VIDEO_EXTS = {".mov", ".mp4", ".m4v"}


LIVE: set = set()  # child processes that are still running
VMAF_SECONDS = 15  # quality is measured on the first 15 seconds, every 5th frame, to keep memory and time low
JOB_TIMEOUT = 1200


def kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def kill_all(*_args) -> None:
    for proc in list(LIVE):
        kill_group(proc)
    LIVE.clear()


def _on_signal(signum, _frame):
    kill_all()
    sys.exit(128 + signum)


def sh(cmd, timeout: int = JOB_TIMEOUT, env: dict | None = None):
    """Run one child in its own process group, with a timeout, and always kill the group afterwards."""
    proc = subprocess.Popen([str(c) for c in cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            env={**os.environ, **(env or {})})
    LIVE.add(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_group(proc)
        out, err = proc.communicate()
        return subprocess.CompletedProcess(proc.args, 124, out, err + "\ntimeout")
    finally:
        kill_group(proc)
        LIVE.discard(proc)
    return subprocess.CompletedProcess(proc.args, proc.returncode, out, err)


def display_dims(path: Path):
    out = sh(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
              "stream=width,height:stream_side_data=rotation", "-of", "json", path])
    s = json.loads(out.stdout)["streams"][0]
    w, h = s["width"], s["height"]
    rot = 0
    for sd in s.get("side_data_list", []):
        if "rotation" in sd:
            rot = int(sd["rotation"])
    return (h, w) if abs(rot) % 180 == 90 else (w, h)


def duration_of(path: Path) -> float:
    out = sh(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path])
    return float(out.stdout.strip())


def has_vmaf() -> bool:
    out = sh(["ffmpeg", "-hide_banner", "-filters"])
    return "libvmaf" in out.stdout


def vmaf(distorted: Path, reference: Path) -> float | None:
    w, h = display_dims(reference)
    k = min(1.0, 1920 / max(w, h))  # never score above 1080p
    w, h = int(w * k) // 2 * 2, int(h * k) // 2 * 2
    cw, ch = (540, 960) if h > w else (960, 540)  # centered crop keeps memory around 100 MB per job
    cw, ch = min(cw, w), min(ch, h)
    graph = (f"[0:v]setpts=N/30/TB,scale={w}:{h}:flags=bicubic,crop={cw}:{ch}[d];"
             f"[1:v]setpts=N/30/TB,scale={w}:{h}:flags=bicubic,crop={cw}:{ch}[r];"
             "[d][r]libvmaf=n_threads=1:n_subsample=5")
    out = sh(["ffmpeg", "-hide_banner", "-loglevel", "info", "-threads", "1", "-t", VMAF_SECONDS, "-i", distorted,
              "-threads", "1", "-t", VMAF_SECONDS, "-i", reference, "-filter_threads", "1",
              "-lavfi", graph, "-f", "null", "-"])
    for line in out.stderr.splitlines():
        if "VMAF score" in line:
            return round(float(line.rsplit(":", 1)[1]), 2)
    return None


def run_skill(src: Path, out_dir: Path, preset: str, software: bool = False) -> dict:
    cmd = ["python3", SCRIPT, "run", src, "--preset", preset, "--out-dir", out_dir, "--timeout", "900"]
    if software:
        cmd.append("--software")
    t0 = time.time()
    proc = sh(cmd, env={"VIDEO_COMPRESSION_THREADS": "2"})
    wall = round(time.time() - t0, 2)
    if not proc.stdout.strip():
        return {"status": "failed", "reason": "no_output", "wall_seconds": wall}
    data = json.loads(proc.stdout)
    item = data["results"][0]
    return {"status": item["status"], "reason": item.get("reason"), "output": item.get("output"),
            "original_bytes": item.get("original_bytes"), "output_bytes": item.get("output_bytes"),
            "encoder": item.get("encoder"), "seconds": item.get("seconds"), "wall_seconds": wall,
            "verify_ok": (item.get("verify") or {}).get("ok")}


def run_naive(src: Path, out_dir: Path, codec: str) -> dict:
    out = out_dir / f"{src.stem}.naive-{codec}.mp4"
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-c:v", codec, "-crf", "23",
           "-threads", "2", "-c:a", "copy"]
    if codec == "libx265":
        cmd += ["-tag:v", "hvc1"]
    cmd.append(out)
    t0 = time.time()
    proc = sh(cmd)
    wall = round(time.time() - t0, 2)
    if proc.returncode != 0 or not out.exists():
        return {"status": "failed", "error": proc.stderr[-300:]}
    return {"status": "encoded", "output": str(out), "original_bytes": src.stat().st_size,
            "output_bytes": out.stat().st_size, "wall_seconds": wall}


def summarize(rows: list[dict]) -> dict:
    done = [r for r in rows if r["status"] in ("compressed", "encoded")]
    savings = [100 * (1 - r["output_bytes"] / r["original_bytes"]) for r in done]
    out = {"clips": len(rows), "encoded": len(done),
           "skipped_already_efficient": sum(1 for r in rows if r.get("reason") == "already_efficient"),
           "skipped_other": sum(1 for r in rows if r["status"] == "skipped" and r.get("reason") != "already_efficient"),
           "failed": sum(1 for r in rows if r["status"] == "failed"),
           "grew": sum(1 for r in done if r["output_bytes"] >= r["original_bytes"])}
    if savings:
        out.update(saving_median=round(statistics.median(savings), 1), saving_min=round(min(savings), 1),
                   saving_max=round(max(savings), 1),
                   seconds_median=round(statistics.median(r["wall_seconds"] for r in done), 1))
    scores = [r["vmaf"] for r in done if r.get("vmaf") is not None]
    if scores:
        out.update(vmaf_median=round(statistics.median(scores), 1), vmaf_min=round(min(scores), 1))
    return out


def measure_set(label: str, clips: list[Path], work: Path, with_vmaf: bool) -> dict:
    result = {"label": label, "clips": [c.name for c in clips], "modes": {}}
    modes = [(p, p, False) for p in PRESETS] + [("balanced-software", "balanced", True)]
    for name, preset, software in modes:
        out_dir = work / label / name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for clip in clips:
            row = {"clip": clip.name, **run_skill(clip, out_dir, preset, software)}
            if with_vmaf and row["status"] == "compressed":
                row["vmaf"] = vmaf(Path(row["output"]), clip)
            rows.append(row)
            print(f"[{label}] {name:18s} {clip.name:22s} {row['status']:10s} "
                  f"{row.get('output_bytes') or ''}", file=sys.stderr, flush=True)
        result["modes"][name] = {"rows": rows, "summary": summarize(rows)}
    for codec in ("libx264", "libx265"):
        name = f"naive-{codec}-crf23"
        out_dir = work / label / name
        out_dir.mkdir(parents=True, exist_ok=True)
        rows = []
        for clip in clips:
            row = {"clip": clip.name, **run_naive(clip, out_dir, codec)}
            if with_vmaf and row["status"] == "encoded":
                row["vmaf"] = vmaf(Path(row["output"]), clip)
            rows.append(row)
            print(f"[{label}] {name:18s} {clip.name:22s} {row['status']:10s} "
                  f"{row.get('output_bytes') or ''}", file=sys.stderr, flush=True)
        result["modes"][name] = {"rows": rows, "summary": summarize(rows)}
    return result


def render_md(data: dict) -> str:
    n = data["environment"]["clip_count"]
    lines = [f"# Benchmark ({data['environment']['date']})", "",
             f"{n} clips recorded on an iPhone 15 Pro (H.264, {data['environment']['clip_description']}). "
             f"Measured with `tools/benchmark.py` on {data['environment']['machine']}. "
             "Sizes are file sizes, saving is `1 - output / original`, time is wall time per file "
             "including verification. VMAF is measured against the file that was compressed, frame-index "
             "aligned, measured on a centered 960x540 crop (540x960 for portrait) at 1080p scale, on the first 15 "
             "seconds of each clip, using every 5th frame, to keep the benchmark light (higher is better). "
             "Absolute VMAF values depend on this method and are not comparable with full-frame scores.", ""]
    for key, title in (("source", f"Set 1: the {n} clips as recorded"),
                       ("reencoded", f"Set 2: the {n} Balanced outputs from set 1 (already compressed once)")):
        s = data["sets"][key]
        lines += [f"## {title}", "",
                  "| Mode | Encoded | Skipped as already efficient | Got bigger | Saving median (min to max) | Median time per file | VMAF median (min) |",
                  "| --- | --- | --- | --- | --- | --- | --- |"]
        for mode, m in s["modes"].items():
            sm = m["summary"]
            saving = (f"{sm['saving_median']}% ({sm['saving_min']}% to {sm['saving_max']}%)"
                      if "saving_median" in sm else "n/a")
            t = f"{sm['seconds_median']} s" if "seconds_median" in sm else "n/a"
            v = f"{sm['vmaf_median']} ({sm['vmaf_min']})" if "vmaf_median" in sm else "n/a"
            lines.append(f"| `{mode}` | {sm['encoded']} of {sm['clips']} | {sm['skipped_already_efficient']} "
                         f"| {sm['grew']} | {saving} | {t} | {v} |")
        lines.append("")
    src, re_ = data["sets"]["source"]["modes"], data["sets"]["reencoded"]["modes"]
    g = lambda d, m, k: d[m]["summary"].get(k)
    lines += ["## What the numbers show", "",
              f"- On clips as recorded, `balanced` saved a median of {g(src, 'balanced', 'saving_median')}% and `premium` "
              f"{g(src, 'premium', 'saving_median')}%. A naive `-crf 23` re-encode saved more "
              f"({g(src, 'naive-libx264-crf23', 'saving_median')}% with libx264, {g(src, 'naive-libx265-crf23', 'saving_median')}% "
              "with libx265) at a similar or higher VMAF. The skill does not claim larger first-pass savings than a plain re-encode.",
              f"- `balanced-software` (libx265) scored a higher VMAF ({g(src, 'balanced-software', 'vmaf_median')}) than "
              f"`balanced` with `hevc_videotoolbox` ({g(src, 'balanced', 'vmaf_median')}) at a similar size, and took "
              f"{g(src, 'balanced-software', 'seconds_median')} s per file instead of {g(src, 'balanced', 'seconds_median')} s.",
              f"- `max-saving` trades quality for size (median VMAF {g(src, 'max-saving', 'vmaf_median')}).",
              f"- On footage that was already compressed once, `premium` and `high` skipped "
              f"{re_['premium']['summary']['skipped_already_efficient']} and {re_['high']['summary']['skipped_already_efficient']} "
              f"of {re_['premium']['summary']['clips']} files as already efficient. `balanced` and `max-saving` still re-encode them "
              "(a second lossy generation) because the sources are above their target bitrate.",
              f"- Naive `-crf 23` made {re_['naive-libx264-crf23']['summary']['grew']} of "
              f"{re_['naive-libx264-crf23']['summary']['clips']} already-compressed files bigger with libx264 and "
              f"{re_['naive-libx265-crf23']['summary']['grew']} with libx265. The skill produced no output larger than its input "
              "(it deletes such an output and reports `not_smaller`).",
              "- Clips are real iPhone recordings, so results for other content (screen recordings, animation, "
              "already-small web videos) will differ.", ""]
    lines += ["Skill modes use `hevc_videotoolbox` unless the name ends in `-software` (libx265). "
              "Naive modes are `ffmpeg -i in -c:v <codec> -crf 23 -c:a copy out.mp4` with default encoder "
              "settings. \"Got bigger\" counts outputs that are as large as or larger than the input. "
              "The skill never keeps such an output.", ""]
    return "\n".join(lines)


def main() -> int:
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clips_dir")
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--no-vmaf", action="store_true")
    ap.add_argument("--stage", choices=["all", "source", "reencoded", "render"], default="all",
                    help="run the sets separately (source, then reencoded, then render) to keep each job short")
    ap.add_argument("--out-json", default=str(ROOT / "skills/video-compression/references/benchmark-results.json"))
    ap.add_argument("--out-md", default=str(ROOT / "skills/video-compression/references/benchmark.md"))
    args = ap.parse_args()

    clips = sorted(p for p in Path(args.clips_dir).iterdir() if p.suffix.lower() in VIDEO_EXTS)
    if not clips:
        print("no clips found", file=sys.stderr)
        return 1
    work = Path(args.work_dir or f"/tmp/vc-bench-{int(time.time())}")
    work.mkdir(parents=True, exist_ok=True)
    with_vmaf = (not args.no_vmaf) and has_vmaf()

    stage_file = lambda name: work / f"{name}.stage.json"
    if args.stage in ("all", "source"):
        source = measure_set("source", clips, work, with_vmaf)
        stage_file("source").write_text(json.dumps(source))
    if args.stage in ("all", "reencoded"):
        source = json.loads(stage_file("source").read_text())
        balanced_outputs = [work / "source" / "balanced" / f"{Path(r['clip']).stem}.compressed.mp4"
                            for r in source["modes"]["balanced"]["rows"] if r["status"] == "compressed"]
        reencoded = measure_set("reencoded", balanced_outputs, work, with_vmaf)
        stage_file("reencoded").write_text(json.dumps(reencoded))
    if args.stage in ("source", "reencoded"):
        return 0
    source = json.loads(stage_file("source").read_text())
    reencoded = json.loads(stage_file("reencoded").read_text())

    dims = {f"{w}x{h}" for w, h in (display_dims(c) for c in clips)}
    total = sum(duration_of(c) for c in clips)
    data = {"environment": {"date": date.today().isoformat(), "clip_count": len(clips),
                            "total_seconds": round(total, 1),
                            "total_bytes": sum(c.stat().st_size for c in clips),
                            "clip_description": f"{', '.join(sorted(dims))}, total {round(total)} s",
                            "machine": f"{platform.machine()} {platform.system()} {platform.release()}",
                            "vmaf": with_vmaf,
                            "ffmpeg": sh(["ffmpeg", "-version"]).stdout.splitlines()[0]},
            "sets": {"source": source, "reencoded": reencoded}}
    for s_ in data["sets"].values():
        for m in s_["modes"].values():
            for row in m["rows"]:
                row.pop("output", None)  # temp paths are not useful in the published results
    Path(args.out_json).write_text(json.dumps(data, indent=1) + "\n")
    Path(args.out_md).write_text(render_md(data))
    print(f"wrote {args.out_json} and {args.out_md}", file=sys.stderr)
    if not args.work_dir:
        shutil.rmtree(work, ignore_errors=True)  # with --work-dir you remove it yourself
    return 0


def run() -> int:
    try:
        return main()
    finally:
        kill_all()


if __name__ == "__main__":
    sys.exit(run())
