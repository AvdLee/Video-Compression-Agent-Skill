"""End-to-end tests for video_compress.py.

The tests call the script the way an agent would (subprocess, JSON on stdout) and compare
its claims against independent ffprobe output. Clips are generated with ffmpeg into a
temporary directory outside the repository, so no videos are ever committed.

Requires ffmpeg and ffprobe on PATH (the tests skip otherwise). Tests that need macOS
avconvert skip on other systems. The network test only runs with VC_TEST_NETWORK=1.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "skills" / "video-compression" / "scripts" / "video_compress.py"
FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")
HAVE_FFMPEG = bool(FFMPEG and FFPROBE)
SKIP_NO_FFMPEG = unittest.skipUnless(HAVE_FFMPEG, "ffmpeg and ffprobe are required")
IS_MAC = sys.platform == "darwin"

TMP: Path
CLIPS: dict[str, Path] = {}


def gen(args: list[str]) -> None:
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], check=True,
                   stdin=subprocess.DEVNULL)


def noisy(size: str, dur: float, rate: int = 30) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={dur},noise=alls=12:allf=t+u"]


SINE = ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]


def setUpModule() -> None:
    global TMP
    if not HAVE_FFMPEG:
        return
    TMP = Path(tempfile.mkdtemp(prefix="vc-tests-"))
    c = CLIPS
    c["h264"] = TMP / "h264_1080.mp4"
    gen(noisy("1920x1080", 4) + SINE + ["-t", "4", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "12M",
                                        "-c:a", "aac", "-b:a", "128k", "-shortest", str(c["h264"])])
    c["no_audio"] = TMP / "no_audio.mp4"
    gen(["-i", str(c["h264"]), "-an", "-c:v", "copy", str(c["no_audio"])])
    c["rotated"] = TMP / "rotated.mp4"
    gen(["-display_rotation", "90", "-i", str(c["h264"]), "-c", "copy", str(c["rotated"])])
    c["hevc_efficient"] = TMP / "hevc_efficient.mp4"
    gen(noisy("1920x1080", 3) + SINE + ["-t", "3", "-c:v", "libx265", "-preset", "veryfast", "-b:v", "2500k",
                                        "-tag:v", "hvc1", "-x265-params", "log-level=error", "-c:a", "aac",
                                        "-b:a", "96k", "-shortest", str(c["hevc_efficient"])])
    c["low_bitrate"] = TMP / "low_bitrate.mp4"
    gen(noisy("1280x720", 3) + ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "500k", str(c["low_bitrate"])])
    c["uhd4k"] = TMP / "uhd_4k.mp4"
    gen(noisy("3840x2160", 2) + SINE + ["-t", "2", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "30M",
                                        "-c:a", "aac", "-shortest", str(c["uhd4k"])])
    c["hdr"] = TMP / "hdr_hlg_10bit.mp4"
    gen(noisy("1280x720", 3) + SINE + ["-t", "3", "-pix_fmt", "yuv420p10le", "-c:v", "libx265", "-preset", "veryfast",
                                       "-b:v", "9M", "-tag:v", "hvc1", "-x265-params",
                                       "log-level=error:colorprim=bt2020:transfer=arib-std-b67:colormatrix=bt2020nc",
                                       "-c:a", "aac", "-shortest", str(c["hdr"])])
    c["short"] = TMP / "short.mp4"
    gen(noisy("1920x1080", 0.6) + ["-c:v", "libx264", "-preset", "veryfast", "-b:v", "12M", str(c["short"])])
    c["vfr"] = TMP / "vfr.mp4"
    gen(noisy("1280x720", 5) + SINE + ["-t", "5", "-vf", "select='gt(random(1),0.35)'", "-fps_mode", "vfr",
                                       "-c:v", "libx264", "-preset", "veryfast", "-b:v", "9M", "-c:a", "aac",
                                       "-shortest", str(c["vfr"])])
    space_dir = TMP / "my videos"
    space_dir.mkdir()
    c["spaces"] = space_dir / "vacation clip (final) v2.mp4"
    shutil.copy(c["h264"], c["spaces"])
    c["corrupt"] = TMP / "corrupt.mp4"
    c["corrupt"].write_bytes(os.urandom(50_000))
    c["truncated"] = TMP / "truncated.mp4"
    c["truncated"].write_bytes(c["h264"].read_bytes()[:60_000])
    c["text"] = TMP / "notes.txt"
    c["text"].write_text("not a video")


def tearDownModule() -> None:
    if HAVE_FFMPEG:
        shutil.rmtree(TMP, ignore_errors=True)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cli(*args: str, env: dict | None = None, python: str | None = None) -> tuple[int, dict | str, str]:
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([python or sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=e,
                       stdin=subprocess.DEVNULL)
    out = p.stdout.strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = out
    return p.returncode, data, p.stderr


def ffprobe(path: Path) -> dict:
    p = subprocess.run([FFPROBE, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True, check=True)
    return json.loads(p.stdout)


def vstream(path: Path) -> dict:
    return next(s for s in ffprobe(path)["streams"] if s["codec_type"] == "video")


def astreams(path: Path) -> list[dict]:
    return [s for s in ffprobe(path)["streams"] if s["codec_type"] == "audio"]


def rotation_of(stream: dict) -> int:
    for sd in stream.get("side_data_list", []) or []:
        if sd.get("side_data_type") == "Display Matrix":
            return int(round(float(sd["rotation"])))
    return 0


def duration_of(path: Path) -> float:
    return float(ffprobe(path)["format"]["duration"])


class Base(unittest.TestCase):
    def run_ok(self, *args, **kw):
        code, data, err = cli(*args, **kw)
        self.assertEqual(code, 0, f"exit {code}: {data} {err}")
        self.assertIsInstance(data, dict, data)
        return data

    def compress(self, key: str, *extra: str, out_dir: Path | None = None) -> dict:
        d = out_dir or Path(tempfile.mkdtemp(dir=TMP))
        res = self.run_ok("run", str(CLIPS[key]), "--out-dir", str(d), *extra)
        return {"res": res, "entry": res["results"][0], "dir": d}


@SKIP_NO_FFMPEG
class DoctorTests(Base):
    def test_doctor_json_and_human(self):
        d = self.run_ok("doctor")
        self.assertEqual(d["tier"], "1")
        self.assertTrue(d["can_run"])
        self.assertIn("hevc_videotoolbox" if IS_MAC else "libx265", d["engines"]["ffmpeg"]["encoders"])
        self.assertTrue(d["app_tip_enabled"])
        code, text, _ = cli("doctor", "--human")
        self.assertEqual(code, 0)
        self.assertIn("Tier 1", text)

    def test_tip_env_opt_out(self):
        d = self.run_ok("doctor", env={"VIDEO_COMPRESSION_SKILL_TIP": "0"})
        self.assertFalse(d["app_tip_enabled"])

    def test_tier3_when_no_engine(self):
        d = self.run_ok("doctor", env={"PATH": "/nonexistent", "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"})
        self.assertEqual(d["tier"], "3")
        self.assertFalse(d["can_run"])
        self.assertIn("system", d["install_hints"])
        self.assertIn("never installs", d["install_hints"]["note"])

    @unittest.skipUnless(IS_MAC and Path("/usr/bin/avconvert").exists(), "macOS avconvert only")
    def test_tier_1b_avconvert_only(self):
        d = self.run_ok("doctor", env={"PATH": "/usr/bin:/bin", "VIDEO_COMPRESSION_DISABLE_ENGINES": "imageio"})
        self.assertEqual(d["tier"], "1b")
        self.assertFalse(d["can_target_size"])

    def test_tier2_ffmpeg_without_ffprobe(self):
        bin_dir = TMP / "ffmpeg_only_bin"
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "ffmpeg"
        if not link.exists():
            link.symlink_to(FFMPEG)
        d = self.run_ok("doctor", env={"PATH": str(bin_dir), "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"})
        self.assertEqual(d["tier"], "2")
        self.assertTrue(d["can_run"])


@SKIP_NO_FFMPEG
class ProbeTests(Base):
    def check_against_ffprobe(self, key: str):
        info = self.run_ok("probe", str(CLIPS[key]))
        v = vstream(CLIPS[key])
        rot = rotation_of(v)
        swapped = abs(rot) in (90, 270)
        self.assertEqual(info["video"]["codec"], v["codec_name"])
        self.assertEqual(info["video"]["display_width"], v["height"] if swapped else v["width"])
        self.assertEqual(info["video"]["display_height"], v["width"] if swapped else v["height"])
        self.assertEqual(info["video"].get("rotation", 0), rot)
        self.assertEqual(info["size_bytes"], CLIPS[key].stat().st_size)
        self.assertAlmostEqual(info["duration_s"], duration_of(CLIPS[key]), delta=0.01)
        self.assertEqual(info["audio"]["streams"], len(astreams(CLIPS[key])))
        return info

    def test_h264(self):
        info = self.check_against_ffprobe("h264")
        self.assertEqual(info["video"]["hdr"], "none")
        self.assertAlmostEqual(info["video"]["fps"], 30, delta=0.1)

    def test_rotation_metadata(self):
        info = self.check_against_ffprobe("rotated")
        self.assertEqual(abs(info["video"]["rotation"]), 90)
        self.assertEqual((info["video"]["display_width"], info["video"]["display_height"]), (1080, 1920))

    def test_no_audio(self):
        info = self.check_against_ffprobe("no_audio")
        self.assertEqual(info["audio"]["streams"], 0)

    def test_hdr_10bit(self):
        info = self.check_against_ffprobe("hdr")
        self.assertEqual(info["video"]["hdr"], "hlg")
        self.assertEqual(info["video"]["bit_depth"], 10)
        self.assertEqual(info["video"]["color_transfer"], "arib-std-b67")

    def test_vfr_flag(self):
        info = self.check_against_ffprobe("vfr")
        self.assertTrue(info["video"].get("vfr"), info["video"])

    def test_short_and_4k(self):
        self.check_against_ffprobe("short")
        self.assertEqual(self.check_against_ffprobe("uhd4k")["video"]["display_width"], 3840)

    def test_path_with_spaces(self):
        self.check_against_ffprobe("spaces")

    def test_human_output(self):
        code, text, _ = cli("probe", str(CLIPS["h264"]), "--human")
        self.assertEqual(code, 0)
        self.assertIn("h264", text)

    def test_stderr_probe_matches_ffprobe_on_tier2(self):
        bin_dir = TMP / "ffmpeg_only_bin"
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "ffmpeg"
        if not link.exists():
            link.symlink_to(FFMPEG)
        env = {"PATH": str(bin_dir), "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"}
        for key in ("h264", "rotated", "hdr", "no_audio"):
            full = self.run_ok("probe", str(CLIPS[key]))
            reduced = self.run_ok("probe", str(CLIPS[key]), env=env)
            self.assertEqual(reduced["probe_source"], "ffmpeg-stderr")
            for f in ("codec", "display_width", "display_height", "bit_depth", "hdr"):
                self.assertEqual(reduced["video"].get(f), full["video"].get(f), f"{key} {f}")
            self.assertEqual(reduced["audio"]["streams"], full["audio"]["streams"])
            self.assertAlmostEqual(reduced["duration_s"], full["duration_s"], delta=0.02)

    def test_errors(self):
        code, data, _ = cli("probe", str(TMP / "does-not-exist.mp4"))
        self.assertEqual((code, data["error"]["code"]), (1, "file_not_found"))
        code, data, _ = cli("probe", str(CLIPS["text"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unsupported_type"))
        code, data, _ = cli("probe", str(CLIPS["corrupt"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unreadable"))
        code, data, _ = cli("probe", str(TMP))
        self.assertEqual((code, data["error"]["code"]), (1, "is_directory"))
        code, text, _ = cli("probe", str(TMP / "nope.mp4"), "--human")
        self.assertEqual(code, 1)
        self.assertIn("file_not_found", text)


@SKIP_NO_FFMPEG
class PlanTests(Base):
    def item(self, key, *extra):
        d = self.run_ok("plan", str(CLIPS[key]), *extra)
        return d["items"][0]

    def test_compress_h264(self):
        it = self.item("h264")
        self.assertEqual(it["action"], "compress")
        self.assertEqual(it["settings"]["codec"], "hevc")
        self.assertGreater(it["estimated_saved_percent"], 30)

    def test_skip_below_bitrate_floor(self):
        it = self.item("low_bitrate")
        self.assertEqual((it["action"], it["reason"]), ("skip", "below_bitrate_floor"))

    def test_skip_efficient_hevc_premium_and_balanced(self):
        it = self.item("hevc_efficient", "--preset", "premium")
        self.assertEqual((it["action"], it["reason"]), ("skip", "premium_efficient_hevc"))
        it = self.item("hevc_efficient", "--preset", "balanced")
        self.assertEqual((it["action"], it["reason"]), ("skip", "already_efficient"))

    def test_resolution_caps(self):
        bal = self.item("uhd4k")
        self.assertEqual(bal["settings"]["resolution"], "1920x1080")
        mx = self.item("uhd4k", "--preset", "max-saving")
        self.assertEqual(mx["settings"]["resolution"], "1280x720")
        prem = self.item("uhd4k", "--preset", "premium")
        self.assertEqual(prem["settings"]["resolution"], "keep")

    def test_rotated_cap_uses_display_dimensions(self):
        it = self.item("rotated", "--preset", "max-saving")
        self.assertEqual(it["settings"]["resolution"], "720x1280")

    def test_preset_bitrates_are_ordered(self):
        rates = [self.item("h264", "--preset", p)["settings"]["video_kbps"]
                 for p in ("premium", "high", "balanced", "max-saving")]
        self.assertEqual(rates, sorted(rates, reverse=True))

    def test_target_mode_and_already_under_target(self):
        it = self.item("h264", "--target-mb", "2")
        self.assertEqual(it["action"], "compress")
        self.assertLess(it["estimated_bytes"], 2_000_000)
        it = self.item("short", "--target-mb", "50")
        self.assertEqual((it["action"], it["reason"]), ("skip", "already_under_target"))

    def test_target_name_and_conflicts(self):
        self.assertEqual(self.item("h264", "--target", "discord")["mode"], "target_10mb")
        code, data, _ = cli("plan", str(CLIPS["h264"]), "--target", "discord", "--target-mb", "5")
        self.assertEqual((code, data["error"]["code"]), (1, "bad_arguments"))
        code, data, _ = cli("plan", str(CLIPS["h264"]), "--target", "imessage")
        self.assertEqual((code, data["error"]["code"]), (1, "unknown_target"))

    def test_hdr_options(self):
        it = self.item("hdr")
        self.assertEqual(it["action"], "compress")
        self.assertTrue(it["settings"]["ten_bit"])
        it = self.item("hdr", "--hdr", "skip")
        self.assertEqual(it["reason"], "hdr_skipped_by_option")
        it = self.item("hdr", "--compat")
        self.assertEqual(it["reason"], "hdr_cannot_be_kept_with_h264_compat")

    def test_folder_plan_counts(self):
        folder = TMP / "plan_folder"
        folder.mkdir(exist_ok=True)
        for name, key in (("a.mp4", "h264"), ("b.mp4", "hevc_efficient"), ("c.mp4", "low_bitrate")):
            shutil.copy(CLIPS[key], folder / name)
        (folder / "notes.txt").write_text("x")
        (folder / "a.compressed.mp4").write_bytes(b"leftover")
        d = self.run_ok("plan", str(folder))
        self.assertEqual(d["summary"]["files"], 3)
        reasons = {Path(i["input"]).name: i.get("reason") for i in d["items"]}
        self.assertEqual(reasons["b.mp4"], "already_efficient")
        self.assertEqual(reasons["c.mp4"], "below_bitrate_floor")
        self.assertEqual(reasons["a.mp4"], "output_exists")  # a.compressed.mp4 already exists, never overwritten

    def test_plan_is_read_only(self):
        before = sha(CLIPS["h264"])
        n = len(list(TMP.iterdir()))
        self.run_ok("plan", str(CLIPS["h264"]))
        self.assertEqual(sha(CLIPS["h264"]), before)
        self.assertEqual(len(list(TMP.iterdir())), n)

    def test_errors(self):
        code, data, _ = cli("plan", str(TMP / "missing.mov"))
        self.assertEqual((code, data["error"]["code"]), (1, "file_not_found"))
        code, data, _ = cli("plan", str(CLIPS["text"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unsupported_type"))
        empty = TMP / "empty_folder"
        empty.mkdir(exist_ok=True)
        code, data, _ = cli("plan", str(empty))
        self.assertEqual((code, data["error"]["code"]), (1, "no_videos"))


@SKIP_NO_FFMPEG
class RunTests(Base):
    def assert_independent(self, key: str, entry: dict, *, codec="hevc", expect_dims=None):
        out = Path(entry["output"])
        self.assertTrue(out.exists())
        v = vstream(out)
        self.assertEqual(v["codec_name"], codec)
        if codec == "hevc":
            self.assertEqual(v["codec_tag_string"], "hvc1")
        self.assertLess(out.stat().st_size, CLIPS[key].stat().st_size)
        self.assertEqual(entry["output_bytes"], out.stat().st_size)  # script report matches the filesystem
        self.assertAlmostEqual(duration_of(out), duration_of(CLIPS[key]), delta=max(0.3, 0.01 * duration_of(CLIPS[key])))
        self.assertEqual(len(astreams(out)), len(astreams(CLIPS[key])))
        self.assertEqual(rotation_of(v), 0)  # rotation is baked into the pixels
        if expect_dims:
            self.assertEqual((v["width"], v["height"]), expect_dims)
        fmt = ffprobe(out)["format"]
        self.assertIn("mp4", fmt["format_name"])
        return v

    def test_h264_to_hevc_balanced(self):
        before = sha(CLIPS["h264"])
        r = self.compress("h264")
        e = r["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assertTrue(e["verify"]["ok"])
        self.assertGreater(e["saved_percent"], 30)
        self.assert_independent("h264", e, expect_dims=(1920, 1080))
        self.assertEqual(sha(CLIPS["h264"]), before)  # original untouched
        self.assertTrue(r["res"]["summary"]["originals_untouched"])
        self.assertEqual([p.name for p in r["dir"].iterdir()], ["h264_1080.compressed.mp4"])  # no leftovers

    def test_faststart(self):
        e = self.compress("h264")["entry"]
        head = Path(e["output"]).read_bytes()[:4096]
        self.assertLess(head.find(b"moov"), 4096 if b"moov" in head else 10**9)
        self.assertIn(b"moov", head)

    def test_no_overwrite_and_idempotent(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        first = self.run_ok("run", str(CLIPS["h264"]), "--out-dir", str(d))["results"][0]
        stamp = Path(first["output"]).read_bytes()
        second = self.run_ok("run", str(CLIPS["h264"]), "--out-dir", str(d))["results"][0]
        self.assertEqual((second["status"], second["reason"]), ("skipped", "output_exists"))
        self.assertEqual(Path(first["output"]).read_bytes(), stamp)

    def test_next_to_source_by_default(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        src = d / "clip one.mp4"
        shutil.copy(CLIPS["h264"], src)
        e = self.run_ok("run", str(src))["results"][0]
        self.assertEqual(Path(e["output"]), d / "clip one.compressed.mp4")
        self.assertTrue(src.exists())

    def test_skipped_files_produce_no_output(self):
        for key, reason in (("low_bitrate", "below_bitrate_floor"), ("hevc_efficient", "already_efficient")):
            r = self.compress(key)
            self.assertEqual((r["entry"]["status"], r["entry"]["reason"]), ("skipped", reason))
            self.assertEqual(list(r["dir"].iterdir()), [])

    def test_portrait_rotation(self):
        e = self.compress("rotated")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assert_independent("rotated", e, expect_dims=(1080, 1920))

    def test_no_audio(self):
        e = self.compress("no_audio")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assert_independent("no_audio", e)
        self.assertEqual(astreams(Path(e["output"])), [])

    def test_4k_balanced_and_max_saving(self):
        e = self.compress("uhd4k")["entry"]
        self.assert_independent("uhd4k", e, expect_dims=(1920, 1080))
        e = self.compress("uhd4k", "--preset", "max-saving")["entry"]
        self.assert_independent("uhd4k", e, expect_dims=(1280, 720))

    def test_hdr_keeps_10bit_and_hlg_tags(self):
        e = self.compress("hdr")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        v = self.assert_independent("hdr", e)
        self.assertEqual(v["pix_fmt"], "yuv420p10le")
        self.assertEqual(v["color_transfer"], "arib-std-b67")
        self.assertEqual(v["color_primaries"], "bt2020")

    def test_hdr_software_path(self):
        e = self.compress("hdr", "--software")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assertEqual(e["encoder"], "libx265")
        v = self.assert_independent("hdr", e)
        self.assertEqual((v["pix_fmt"], v["color_transfer"]), ("yuv420p10le", "arib-std-b67"))

    def test_hdr_with_h264_compat_is_skipped_not_degraded(self):
        r = self.compress("hdr", "--compat")["entry"]
        self.assertEqual((r["status"], r["reason"]), ("skipped", "hdr_cannot_be_kept_with_h264_compat"))

    def test_software_sdr_libx265(self):
        e = self.compress("h264", "--software")["entry"]
        self.assertEqual((e["status"], e["encoder"]), ("compressed", "libx265"))
        self.assert_independent("h264", e)

    def test_h264_compat(self):
        e = self.compress("h264", "--compat")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        v = self.assert_independent("h264", e, codec="h264")
        self.assertEqual(v["pix_fmt"], "yuv420p")

    def test_vfr_source(self):
        e = self.compress("vfr")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assertIn("variable_frame_rate_source", e["warnings"])
        self.assert_independent("vfr", e)

    def test_very_short_clip(self):
        e = self.compress("short")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assert_independent("short", e)

    def test_path_with_spaces(self):
        before = sha(CLIPS["spaces"])
        e = self.run_ok("run", str(CLIPS["spaces"]))["results"][0]
        self.assertEqual(e["status"], "compressed", e)
        self.assertEqual(Path(e["output"]).name, "vacation clip (final) v2.compressed.mp4")
        self.assertEqual(sha(CLIPS["spaces"]), before)
        self.assertTrue(Path(e["output"]).exists())

    def test_metadata_preserved(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        src = d / "meta.mp4"
        gen(["-i", str(CLIPS["h264"]), "-c", "copy", "-metadata", "creation_time=2026-03-28T13:32:08Z",
             "-metadata", "title=My clip", str(src)])
        e = self.run_ok("run", str(src))["results"][0]
        tags = ffprobe(Path(e["output"]))["format"]["tags"]
        self.assertEqual(tags.get("title"), "My clip")
        self.assertTrue(tags.get("creation_time", "").startswith("2026-03-28"))

    def test_target_size_mode_under_limit(self):
        for mb in (3, 6):
            r = self.compress("h264", "--target-mb", str(mb))
            e = r["entry"]
            self.assertEqual(e["status"], "compressed", e)
            size = Path(e["output"]).stat().st_size
            self.assertLessEqual(size, mb * 1_000_000)
            self.assertGreater(size, mb * 1_000_000 * 0.4)  # uses the budget, not far below it
            self.assertAlmostEqual(duration_of(Path(e["output"])), 4.0, delta=0.3)

    def test_target_size_two_pass_software(self):
        e = self.compress("h264", "--target-mb", "4", "--software")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assertLessEqual(Path(e["output"]).stat().st_size, 4_000_000)
        self.assertEqual(e["encoder"], "libx265")

    def test_target_h264_compat_discord(self):
        e = self.compress("h264", "--target", "discord", "--compat")["entry"]
        self.assertEqual(e["status"], "compressed", e)
        self.assertLessEqual(Path(e["output"]).stat().st_size, 10_000_000)
        self.assert_independent("h264", e, codec="h264")

    def test_batch_folder(self):
        folder = Path(tempfile.mkdtemp(dir=TMP)) / "batch"
        folder.mkdir()
        for name, key in (("one.mp4", "h264"), ("two.mp4", "no_audio"), ("three.mp4", "hevc_efficient"),
                          ("bad.mp4", "corrupt")):
            shutil.copy(CLIPS[key], folder / name)
        (folder / "readme.txt").write_text("skip me")
        (folder / "old.compressed.mp4").write_bytes(b"previous output")
        hashes = {p.name: sha(p) for p in folder.iterdir()}
        code, data, _ = cli("run", str(folder))
        self.assertEqual(code, 1)  # the corrupt file counts as a failure
        s = data["summary"]
        self.assertEqual((s["compressed"], s["skipped"], s["failed"]), (2, 1, 1))
        by = {Path(r["input"]).name: r for r in data["results"]}
        self.assertEqual(by["bad.mp4"]["reason"], "unreadable")
        self.assertEqual(by["three.mp4"]["reason"], "already_efficient")
        self.assertEqual(set(by), {"one.mp4", "two.mp4", "three.mp4", "bad.mp4"})
        for name, digest in hashes.items():
            self.assertEqual(sha(folder / name), digest, f"{name} was modified")  # originals + old output untouched
        self.assertTrue((folder / "one.compressed.mp4").exists())
        self.assertTrue((folder / "two.compressed.mp4").exists())
        self.assertFalse(any(".partial" in p.name for p in folder.iterdir()))

    def test_recursive_flag(self):
        base = Path(tempfile.mkdtemp(dir=TMP))
        (base / "sub").mkdir()
        shutil.copy(CLIPS["short"], base / "sub" / "deep.mp4")
        code, data, _ = cli("run", str(base))
        self.assertEqual((code, data["error"]["code"]), (1, "no_videos"))
        data = self.run_ok("run", str(base), "--recursive")
        self.assertEqual(data["summary"]["compressed"], 1)

    def test_corrupt_and_unsupported_single_file(self):
        code, data, _ = cli("run", str(CLIPS["corrupt"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unreadable"))
        code, data, _ = cli("run", str(CLIPS["text"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unsupported_type"))
        code, data, _ = cli("run", str(CLIPS["truncated"]))
        self.assertEqual(code, 1)
        self.assertFalse((TMP / "truncated.compressed.mp4").exists())
        self.assertFalse((TMP / "corrupt.compressed.mp4").exists())

    def test_dry_run_matches_plan(self):
        d = self.run_ok("run", str(CLIPS["h264"]), "--dry-run")
        self.assertTrue(d["dry_run"])
        self.assertFalse((TMP / "h264_1080.compressed.mp4").exists())

    def test_human_output(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        code, text, err = cli("run", str(CLIPS["h264"]), "--out-dir", str(d), "--human")
        self.assertEqual(code, 0)
        self.assertIn("compressed, saved", text)
        self.assertIn("%", err)  # progress goes to stderr, not stdout

    def test_json_stdout_is_pure(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        code, data, err = cli("run", str(CLIPS["h264"]), "--out-dir", str(d))
        self.assertIsInstance(data, dict)  # stdout parsed as one JSON document

    def test_force_overrides_skip_but_not_the_not_smaller_gate(self):
        r = self.compress("low_bitrate", "--force")["entry"]
        self.assertIn(r["status"], ("compressed", "skipped"))
        if r["status"] == "skipped":
            self.assertEqual(r["reason"], "not_smaller")
        else:
            self.assertLess(r["output_bytes"], CLIPS["low_bitrate"].stat().st_size)

    def test_compact_json_is_small(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        p = subprocess.run([sys.executable, str(SCRIPT), "run", str(CLIPS["h264"]), "--out-dir", str(d)],
                           capture_output=True, text=True)
        self.assertLess(len(p.stdout), 1500)
        self.assertNotIn("\n  ", p.stdout)


@SKIP_NO_FFMPEG
class VerifyTests(Base):
    def test_verify_good_pair(self):
        e = self.compress("h264")["entry"]
        d = self.run_ok("verify", str(CLIPS["h264"]), e["output"])
        self.assertTrue(d["ok"])
        self.assertEqual(d["saved_bytes"], CLIPS["h264"].stat().st_size - Path(e["output"]).stat().st_size)
        self.assertEqual(set(d["checks"]), {"duration", "audio_streams", "video_present", "fps", "resolution", "smaller"})
        code, text, _ = cli("verify", str(CLIPS["h264"]), e["output"], "--human")
        self.assertIn("PASS duration", text)

    def test_verify_detects_bigger_output(self):
        code, data, _ = cli("verify", str(CLIPS["low_bitrate"]), str(CLIPS["h264"]))
        self.assertEqual(code, 1)  # a failed verification exits 1 and still reports every check
        self.assertFalse(data["ok"])
        self.assertFalse(data["checks"]["smaller"]["ok"])

    def test_verify_detects_missing_audio_and_duration_mismatch(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        bad = d / "bad.mp4"
        gen(["-i", str(CLIPS["h264"]), "-t", "1.5", "-an", "-c:v", "libx264", "-b:v", "500k", str(bad)])
        code, data, _ = cli("verify", str(CLIPS["h264"]), str(bad))
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertFalse(data["checks"]["duration"]["ok"])
        self.assertFalse(data["checks"]["audio_streams"]["ok"])

    def test_verify_detects_orientation_change(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        bad = d / "wrong_orientation.mp4"
        gen(["-i", str(CLIPS["rotated"]), "-vf", "transpose=0", "-c:v", "libx264", "-b:v", "1M", "-c:a", "aac", str(bad)])
        # original displays 1080x1920; transposing twice would match, once flips it back to 1920x1080
        code, data, _ = cli("verify", str(CLIPS["rotated"]), str(bad))
        self.assertEqual(code, 1)
        self.assertFalse(data["checks"]["resolution"]["ok"])

    def test_verify_errors(self):
        code, data, _ = cli("verify", str(CLIPS["h264"]), str(TMP / "missing.mp4"))
        self.assertEqual((code, data["error"]["code"]), (1, "file_not_found"))
        code, data, _ = cli("verify", str(CLIPS["h264"]), str(CLIPS["corrupt"]))
        self.assertEqual((code, data["error"]["code"]), (1, "unreadable"))


class AdviseTests(Base):
    """advise is pure math and must work without any engine."""
    NO_ENGINE = {"PATH": "/nonexistent", "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"}

    def test_math_matches_formula(self):
        d = self.run_ok("advise", "--duration", "60", "--width", "1920", "--height", "1080", "--target-mb", "25",
                        env=self.NO_ENGINE)
        total_kbps = 25_000_000 * 0.95 * 8 / 60 / 1000
        self.assertAlmostEqual(d["video_kbps"] + d["audio_kbps"], total_kbps, delta=2)
        self.assertEqual(d["resolution"], "1920x1080")
        for os_name in ("macos", "linux", "windows"):
            self.assertIn(f'-b:v {d["video_kbps"]}k', d["commands"][os_name])
        self.assertIn("NUL", d["commands"]["windows"])
        self.assertIn("/dev/null", d["commands"]["linux"])
        self.assertIn("-tag:v hvc1", d["commands"]["macos"])
        self.assertIn("faststart", d["commands"]["macos"])

    def test_compat_uses_x264(self):
        d = self.run_ok("advise", "--duration", "30", "--width", "1280", "--height", "720", "--target", "discord",
                        "--compat", env=self.NO_ENGINE)
        self.assertEqual(d["codec"], "h264")
        self.assertIn("libx264", d["commands"]["linux"])
        self.assertNotIn("hvc1", d["commands"]["linux"])

    def test_resolution_steps_down_for_tiny_budgets(self):
        d = self.run_ok("advise", "--duration", "300", "--width", "3840", "--height", "2160", "--target-mb", "10",
                        env=self.NO_ENGINE)
        w, h = (int(x) for x in d["resolution"].split("x"))
        self.assertLess(max(w, h), 3840)
        self.assertIn("scale=", d["commands"]["linux"])

    def test_portrait_orientation_kept(self):
        d = self.run_ok("advise", "--duration", "300", "--width", "1080", "--height", "1920", "--target-mb", "9",
                        env=self.NO_ENGINE)
        w, h = (int(x) for x in d["resolution"].split("x"))
        self.assertGreater(h, w)

    def test_infeasible_target(self):
        d = self.run_ok("advise", "--duration", "7200", "--width", "1920", "--height", "1080", "--target-mb", "1",
                        env=self.NO_ENGINE)
        self.assertFalse(d["feasible"])

    def test_platform_targets_match_limits(self):
        for name, mb in {"discord": 10, "whatsapp": 64, "email": 25, "slack": 1000}.items():
            d = self.run_ok("advise", "--duration", "120", "--width", "1920", "--height", "1080", "--target", name,
                            env=self.NO_ENGINE)
            self.assertEqual(d["target_mb"], mb)

    def test_errors(self):
        code, data, _ = cli("advise", "--duration", "10", env=self.NO_ENGINE)
        self.assertEqual((code, data["error"]["code"]), (1, "bad_arguments"))
        code, data, _ = cli("advise", "--duration", "0", "--target-mb", "5", env=self.NO_ENGINE)
        self.assertEqual((code, data["error"]["code"]), (1, "bad_arguments"))
        code, data, _ = cli("advise", "--duration", "10", "--target", "imessage", env=self.NO_ENGINE)
        self.assertEqual((code, data["error"]["code"]), (1, "unknown_target"))
        code, _, _ = cli("advise", "--duration", "abc", "--target-mb", "5", env=self.NO_ENGINE)
        self.assertEqual(code, 2)

    def test_human_output(self):
        code, text, _ = cli("advise", "--duration", "60", "--width", "1920", "--height", "1080", "--target-mb", "25",
                            "--human", env=self.NO_ENGINE)
        self.assertEqual(code, 0)
        self.assertIn("windows:", text)

    def test_platform_limits_doc_matches_script(self):
        doc = (REPO / "skills" / "video-compression" / "references" / "platform-limits.md").read_text()
        for name, mb in {"discord": 10, "whatsapp": 64, "email": 25, "slack": 1000}.items():
            self.assertIn(f"`{name}`", doc)
            self.assertIn(f"{mb} MB", doc)


@SKIP_NO_FFMPEG
class NoFfmpegTests(Base):
    NO_FFMPEG = {"PATH": "/usr/bin:/bin", "VIDEO_COMPRESSION_DISABLE_ENGINES": "imageio"}
    NO_ENGINE = {"PATH": "/nonexistent", "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"}

    def test_no_engine_gives_hints_and_never_downloads(self):
        code, data, _ = cli("run", str(CLIPS["h264"]), env=self.NO_ENGINE)
        self.assertEqual((code, data["error"]["code"]), (1, "no_engine"))
        self.assertIn("doctor", data["error"]["hint"])
        self.assertFalse(any(TMP.glob("*.compressed.mp4")))

    @unittest.skipUnless(IS_MAC and Path("/usr/bin/avconvert").exists(), "macOS avconvert only")
    def test_avconvert_fallback_end_to_end(self):
        d = Path(tempfile.mkdtemp(dir=TMP))
        src = d / "src.mp4"
        shutil.copy(CLIPS["h264"], src)
        before = sha(src)
        res = self.run_ok("run", str(src), "--preset", "balanced", env=self.NO_FFMPEG)
        self.assertEqual(res["engine"], "avconvert")
        e = res["results"][0]
        self.assertEqual(e["status"], "compressed", e)
        out = Path(e["output"])
        self.assertTrue(out.exists())
        self.assertLess(out.stat().st_size, src.stat().st_size)  # real size check
        self.assertEqual(sha(src), before)
        v = vstream(out)  # independent check with the system ffprobe, outside the script
        self.assertEqual(v["codec_name"], "hevc")
        self.assertAlmostEqual(duration_of(out), 4.0, delta=0.3)
        self.assertEqual(len(astreams(out)), 1)
        self.assertEqual([p.name for p in d.iterdir() if ".partial" in p.name], [])

    @unittest.skipUnless(IS_MAC and Path("/usr/bin/avconvert").exists(), "macOS avconvert only")
    def test_avconvert_limits_are_explicit(self):
        code, data, _ = cli("plan", str(CLIPS["h264"]), "--target-mb", "5", env=self.NO_FFMPEG)
        self.assertEqual(code, 0)
        self.assertEqual(data["items"][0]["reason"], "engine_cannot_target_size")
        data = self.run_ok("plan", str(CLIPS["hdr"]), env=self.NO_FFMPEG)
        self.assertEqual(data["items"][0]["reason"], "hdr_unsupported_on_avconvert_path")
        data = self.run_ok("probe", str(CLIPS["h264"]), env=self.NO_FFMPEG)
        self.assertEqual(data["probe_source"], "avmediainfo")
        self.assertEqual((data["video"]["display_width"], data["video"]["display_height"]), (1920, 1080))

    def test_tier2_run_without_ffprobe(self):
        bin_dir = TMP / "ffmpeg_only_bin"
        bin_dir.mkdir(exist_ok=True)
        link = bin_dir / "ffmpeg"
        if not link.exists():
            link.symlink_to(FFMPEG)
        env = {"PATH": str(bin_dir), "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert,imageio"}
        d = Path(tempfile.mkdtemp(dir=TMP))
        res = self.run_ok("run", str(CLIPS["h264"]), "--out-dir", str(d), env=env)
        self.assertEqual(res["tier"], "2")
        e = res["results"][0]
        self.assertEqual(e["status"], "compressed", e)
        out = Path(e["output"])
        self.assertEqual(vstream(out)["codec_name"], "hevc")
        self.assertEqual(vstream(out)["codec_tag_string"], "hvc1")
        self.assertLess(out.stat().st_size, CLIPS["h264"].stat().st_size)

    def test_sandbox_limits_stop_early(self):
        # Only the imageio engine kind enforces the 120 s / 450 MB sandbox limits.
        sys.path.insert(0, str(SCRIPT.parent))
        import video_compress as vc  # noqa: E402
        eng = {"kind": "imageio", "ffmpeg": "x", "encoders": ["libx264", "libx265"], "tier": "2"}
        info = {"path": "/x.mp4", "size_bytes": 10**9, "duration_s": 600, "bitrate_bps": 15_000_000,
                "video": {"codec": "h264", "display_width": 1920, "display_height": 1080, "fps": 30,
                          "bitrate_bps": 14_800_000, "bit_depth": 8, "hdr": "none"},
                "audio": {"streams": 1, "codec": "aac", "bitrate_bps": 128000}, "other_streams": {}, "flags": []}
        ns = vc.build_parser().parse_args(["plan", "/x.mp4"])
        ns.target_bytes = None
        item = vc.decide(info, eng, ns)
        self.assertEqual(item["reason"], "too_large_for_sandbox")


class NetworkTests(unittest.TestCase):
    """Runs only with VC_TEST_NETWORK=1: installs imageio-ffmpeg into a temp skill home."""

    @unittest.skipUnless(os.environ.get("VC_TEST_NETWORK") == "1" and HAVE_FFMPEG, "set VC_TEST_NETWORK=1")
    def test_allow_download_in_temp_venv(self):
        venv = TMP / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        py = str(venv / ("Scripts" if os.name == "nt" else "bin") / "python")
        home = TMP / "skillhome"
        env = {"PATH": "/usr/bin:/bin", "VIDEO_COMPRESSION_SKILL_HOME": str(home),
               "VIDEO_COMPRESSION_DISABLE_ENGINES": "avconvert"}
        code, data, _ = cli("run", str(CLIPS["h264"]), "--out-dir", str(TMP / "dl-out"), env=env, python=py)
        self.assertEqual((code, data["error"]["code"]), (1, "no_engine"))  # no download without the flag
        code, data, err = cli("run", str(CLIPS["h264"]), "--out-dir", str(TMP / "dl-out"), "--allow-download",
                              env=env, python=py)
        self.assertEqual(code, 0, f"{data} {err}")
        self.assertEqual(data["tier"], "2")
        e = data["results"][0]
        self.assertEqual(e["status"], "compressed", e)
        self.assertEqual(vstream(Path(e["output"]))["codec_name"] in ("hevc", "h264"), True)
        self.assertTrue(str(home) in subprocess.run([py, str(SCRIPT), "doctor"], capture_output=True, text=True,
                                                     env={**os.environ, **env}).stdout)

class RepoHygieneTests(unittest.TestCase):
    """Checks on the shipped files that need no ffmpeg."""

    def test_skill_frontmatter(self):
        text = (REPO / "skills" / "video-compression" / "SKILL.md").read_text()
        front = text.split("---")[1]
        self.assertIn("name: video-compression", front)
        desc = " ".join(line.strip() for line in front.split("description:")[1].splitlines())
        self.assertLess(len(desc), 1024)

    def test_trigger_evals_shape(self):
        data = json.loads((REPO / "evals" / "trigger-evals.json").read_text())
        self.assertGreaterEqual(sum(1 for d in data if d["should_trigger"]), 5)
        self.assertGreaterEqual(sum(1 for d in data if not d["should_trigger"]), 3)
        evals = json.loads((REPO / "evals" / "evals.json").read_text())
        self.assertEqual(evals["skill_name"], "video-compression")
        for e in evals["evals"]:
            self.assertTrue(e["prompt"] and e["expectations"])

    def test_manifests_valid_and_consistent(self):
        manifests = ["plugin.json", ".claude-plugin/plugin.json", ".cursor-plugin/plugin.json",
                     ".codex-plugin/plugin.json"]
        versions = set()
        for m in manifests:
            data = json.loads((REPO / m).read_text())
            self.assertEqual(data["name"], "video-compression")
            versions.add(data["version"])
        self.assertEqual(len(versions), 1)
        codex = json.loads((REPO / ".codex-plugin/plugin.json").read_text())["interface"]
        self.assertLessEqual(len(codex["displayName"]), 30)
        self.assertLessEqual(len(codex["shortDescription"]), 30)
        for path in (REPO / "plugin.json", REPO / ".codex-plugin/plugin.json"):
            self.assertNotIn("requires_local_executor", path.read_text())

    def test_no_em_dashes_in_public_text(self):
        for path in list(REPO.glob("*.md")) + list((REPO / "skills").rglob("*.md")) + list((REPO / "docs").rglob("*.md")):
            self.assertNotIn("\u2014", path.read_text(), str(path))

    def test_app_link_format(self):
        text = (REPO / "skills/video-compression/references/app-handoff.md").read_text()
        self.assertIn("https://apps.apple.com/app/id6810118769?pt=117264678&ct=agent-skill_", text)


if __name__ == "__main__":
    unittest.main()
