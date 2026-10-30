#!/usr/bin/env python3
"""Video compression helper for coding agents (Python 3 standard library only).

Subcommands (compact JSON on stdout by default, `--human` for readable text):

  doctor                      detect the capability tier, engines and install hints
  probe   <file>              codec, resolution, rotation, fps, bitrate, HDR, audio
  plan    <file|folder>       decide settings per file and skip files that would not benefit
  run     <file|folder>       compress, verify, and keep the original untouched
  verify  <original> <output> independent integrity and size checks
  advise  --duration S ...    bitrate math and exact commands, no execution

Safety rules: originals are never modified, moved or deleted; outputs are written
next to the source (or into --out-dir) as `<name>.compressed.mp4` and never
overwrite an existing file; nothing is installed; there is no telemetry; the
network is only used when you pass --allow-download.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

SKILL_NAME = "video-compression"
SCRIPT_VERSION = "1.0.0"
OUTPUT_SUFFIX = ".compressed.mp4"
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".ts",
              ".wmv", ".flv", ".3gp", ".mpg", ".mpeg"}

# --- Rules mirrored from the Video Compressor app's preset logic (see references/presets.md) ---
MIN_BITRATE = 1_000_000                 # below this a source is already optimized
PREMIUM_EFFICIENT_HEVC_MAX = 5_000_000  # Premium skips HEVC at or below this
MIN_MEANINGFUL_SAVING = 0.10            # fraction of the original that must be reclaimed
PRESETS = {
    # density_factor: multiplier on the base density; max_dim: longest-edge cap;
    # ceiling: share of the source bitrate the target may not exceed;
    # max_share: output must stay below this share of the source size.
    "premium":    {"density_factor": 1.75, "max_dim": None, "ceiling": 0.8, "max_share": 0.85},
    "high":       {"density_factor": 1.40, "max_dim": None, "ceiling": 0.6, "max_share": 1.0},
    "balanced":   {"density_factor": 1.14, "max_dim": 1920, "ceiling": 0.6, "max_share": 1.0},
    "max-saving": {"density_factor": 0.87, "max_dim": 1280, "ceiling": 0.6, "max_share": 1.0},
}
DEFAULT_PRESET = "balanced"

# --- Skill-owned calibration (NOT taken from the app; see references/presets.md) ---
BASE_BPP = 0.08          # bits per pixel per frame for HEVC at the reference quality
CODEC_EFFICIENCY = {"hevc": 1.0, "av1": 1.2, "vp9": 1.0, "h264": 0.7}  # vs HEVC, rough
DEFAULT_EFFICIENCY = 0.6  # older codecs (mpeg4, mpeg2, wmv, ...)
H264_BPP_FACTOR = 1.4     # H.264 needs about 1.4x the HEVC density for similar quality
TARGET_SAFETY = 0.95      # aim below the requested size to absorb container and VBR drift
MIN_VIDEO_BPS_FOR_TARGET = 100_000
LADDER = [2160, 1440, 1080, 720, 540, 480, 360]
MIN_TARGET_BPP = 0.03     # below this bits-per-pixel the resolution steps down the ladder
SANDBOX_MAX_SECONDS = 120
SANDBOX_MAX_BYTES = 450 * 1000 * 1000

# Verified against official documentation; see references/platform-limits.md.
PLATFORM_LIMITS_MB = {"discord": 10, "whatsapp": 64, "email": 25, "slack": 1000}

COPYABLE_AUDIO = {"aac", "mp3", "ac3", "eac3", "alac"}
AVCONVERT = "/usr/bin/avconvert"
AVMEDIAINFO = "/usr/bin/avmediainfo"
AVCONVERT_PRESETS = {  # fixed presets; no exact size targeting
    "premium": "PresetHEVCHighestQuality",
    "high": "PresetHEVC3840x2160",
    "balanced": "PresetHEVC1920x1080",
    "max-saving": "Preset1280x720",
}


class SkillError(Exception):
    def __init__(self, code: str, message: str, hint: str | None = None):
        super().__init__(message)
        self.code, self.message, self.hint = code, message, hint


# ---------------------------------------------------------------- helpers

def skill_home() -> Path:
    env = os.environ.get("VIDEO_COMPRESSION_SKILL_HOME")
    return Path(env) if env else Path(__file__).resolve().parent.parent / ".engine"


def human_size(n: float | int | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1000 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000
    return f"{n:.1f} GB"


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def compact(obj):
    """Drop None values and empty containers recursively so JSON stays small."""
    if isinstance(obj, dict):
        out = {k: compact(v) for k, v in obj.items()}
        return {k: v for k, v in out.items() if v is not None and v != [] and v != {}}
    if isinstance(obj, list):
        return [compact(v) for v in obj]
    return obj


def run_capture(cmd: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           errors="replace", stdin=subprocess.DEVNULL)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def even(n: float) -> int:
    n = int(round(n))
    return max(2, n - (n % 2))


# ---------------------------------------------------------------- engines

def _disabled() -> set[str]:
    """VIDEO_COMPRESSION_DISABLE_ENGINES=avconvert,imageio hides engines (testing, forcing advisor mode)."""
    return {x.strip() for x in os.environ.get("VIDEO_COMPRESSION_DISABLE_ENGINES", "").split(",") if x.strip()}


def _imageio_exe() -> str | None:
    if "imageio" in _disabled():
        return None
    # 1) skill-local cache from an earlier --allow-download, 2) an importable imageio_ffmpeg
    cache = skill_home() / "pylib" / "imageio_ffmpeg" / "binaries"
    if cache.is_dir():
        for f in sorted(cache.iterdir()):
            if f.name.startswith("ffmpeg") and f.is_file():
                return str(f)
    try:
        import imageio_ffmpeg  # type: ignore
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception:
        pass
    return None


def encoders_of(ffmpeg: str) -> set[str]:
    rc, out, _ = run_capture([ffmpeg, "-hide_banner", "-encoders"], timeout=30)
    found = set()
    for name in ("hevc_videotoolbox", "libx265", "libx264", "h264_videotoolbox", "aac"):
        if re.search(rf"^\s*[VASFXBD.]{{6}}\s+{name}\s", out, re.M):
            found.add(name)
    return found


def ffmpeg_version(ffmpeg: str) -> str | None:
    rc, out, err = run_capture([ffmpeg, "-hide_banner", "-version"], timeout=15)
    m = re.search(r"version\s+(\S+)", out or err)
    return m.group(1) if m else None


def detect_engines(allow_download: bool = False) -> dict:
    """Describe what is available. Never installs anything."""
    system_ffmpeg = shutil.which("ffmpeg")
    system_ffprobe = shutil.which("ffprobe")
    imageio = _imageio_exe()
    avconvert = AVCONVERT if (sys.platform == "darwin" and Path(AVCONVERT).exists()
                              and "avconvert" not in _disabled()) else None
    info: dict = {"system_ffmpeg": system_ffmpeg, "system_ffprobe": system_ffprobe,
                  "imageio_ffmpeg": imageio, "avconvert": avconvert}
    if system_ffmpeg and system_ffprobe:
        info["tier"], info["kind"] = "1", "ffmpeg"
        info["ffmpeg"], info["ffprobe"] = system_ffmpeg, system_ffprobe
    elif imageio or system_ffmpeg:
        info["tier"], info["kind"] = "2", "imageio" if not system_ffmpeg else "ffmpeg"
        info["ffmpeg"], info["ffprobe"] = system_ffmpeg or imageio, system_ffprobe
    elif avconvert:
        info["tier"], info["kind"] = "1b", "avconvert"
    else:
        info["tier"], info["kind"] = "3", "none"
    return info


def install_hints() -> dict:
    sysname = platform.system()
    hints = {"python_user_space": f'{sys.executable} -m pip install --user imageio-ffmpeg '
                                  "(static ffmpeg without ffprobe; or pass --allow-download to this script)"}
    if sysname == "Darwin":
        hints["system"] = "brew install ffmpeg"
    elif sysname == "Windows":
        hints["system"] = "winget install Gyan.FFmpeg"
    else:
        hints["system"] = "sudo apt install ffmpeg  (Debian/Ubuntu) | sudo dnf install ffmpeg | sudo pacman -S ffmpeg"
    hints["note"] = "This script never installs software by itself. Ask the user before running any install command."
    return hints


def download_engine() -> str:
    """Install imageio-ffmpeg into a skill-local directory (user space, no sudo). Network call."""
    target = skill_home() / "pylib"
    target.mkdir(parents=True, exist_ok=True)
    log(f"downloading imageio-ffmpeg into {target} (network, --allow-download)")
    rc, out, err = run_capture([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                                "--quiet", "--target", str(target), "imageio-ffmpeg"], timeout=600)
    if rc != 0:
        raise SkillError("download_failed", "pip could not install imageio-ffmpeg: " + (err or out)[-300:],
                         "Install ffmpeg yourself: " + install_hints()["system"])
    exe = _imageio_exe()
    if not exe:
        raise SkillError("download_failed", "imageio-ffmpeg installed but no ffmpeg binary was found in it")
    if sys.platform != "win32":
        os.chmod(exe, 0o755)
    return exe


def resolve_engine(allow_download: bool = False, need_ffmpeg: bool = True) -> dict:
    """Resolution order: system ffmpeg, cached/installed imageio ffmpeg, avconvert (macOS),
    then (only with --allow-download) a user-space static binary. With --allow-download
    the download wins over avconvert because the user asked for a real ffmpeg."""
    eng = detect_engines()
    if eng["kind"] in ("ffmpeg", "imageio"):
        return _finish_engine(eng)
    if allow_download:
        exe = download_engine()
        eng = detect_engines()
        eng["ffmpeg"], eng["kind"], eng["tier"], eng["downloaded"] = exe, "imageio", "2", True
        return _finish_engine(eng)
    if eng["kind"] == "avconvert":
        return eng
    raise SkillError("no_engine", "No video engine found (ffmpeg, or macOS avconvert).",
                     "Run `doctor` for install hints, use `advise` for commands, or pass --allow-download.")


def _finish_engine(eng: dict) -> dict:
    eng["encoders"] = sorted(encoders_of(eng["ffmpeg"]))
    eng["ffmpeg_version"] = ffmpeg_version(eng["ffmpeg"])
    return eng


# ---------------------------------------------------------------- probing

def _fraction(s: str | None) -> float | None:
    if not s or s in ("0/0", "N/A"):
        return None
    try:
        if "/" in s:
            a, b = s.split("/")
            return float(a) / float(b) if float(b) else None
        return float(s)
    except ValueError:
        return None


def _int(s) -> int | None:
    try:
        return int(float(s))
    except (TypeError, ValueError):
        return None


def probe_ffprobe(path: Path, ffprobe: str) -> dict:
    rc, out, err = run_capture([ffprobe, "-v", "error", "-show_format", "-show_streams",
                                "-of", "json", str(path)], timeout=60)
    if rc != 0:
        raise SkillError("unreadable", f"ffprobe could not read the file: {(err or out).strip()[-200:]}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise SkillError("unreadable", "ffprobe returned invalid output")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    vstreams = [s for s in streams if s.get("codec_type") == "video"
                and not s.get("disposition", {}).get("attached_pic")]
    if not vstreams:
        raise SkillError("no_video_stream", "The file has no video stream.")
    v = vstreams[0]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    size = int(fmt.get("size") or path.stat().st_size)
    duration = _fraction(fmt.get("duration")) or _fraction(v.get("duration"))
    width, height = _int(v.get("width")), _int(v.get("height"))
    rotation = 0
    dovi = None
    stereo = False
    for sd in v.get("side_data_list", []) or []:
        t = sd.get("side_data_type", "")
        if t == "Display Matrix" and sd.get("rotation") is not None:
            rotation = int(round(float(sd["rotation"])))
        if "DOVI" in t or "Dolby Vision" in t:
            dovi = {"profile": sd.get("dv_profile"), "compat_id": sd.get("dv_bl_signal_compatibility_id")}
        if "Stereo 3D" in t or "view" in t.lower():
            stereo = True
    if not rotation and (v.get("tags") or {}).get("rotate"):
        rotation = -int(float(v["tags"]["rotate"]))
    swapped = abs(rotation) in (90, 270)
    avg, rfr = _fraction(v.get("avg_frame_rate")), _fraction(v.get("r_frame_rate"))
    fps = avg or rfr
    vfr = bool(avg and rfr and abs(avg - rfr) / rfr > 0.02)
    pix_fmt = v.get("pix_fmt")
    depth = _int(v.get("bits_per_raw_sample")) or (10 if pix_fmt and re.search(r"10(le|be)|p010", pix_fmt)
                                                  else 12 if pix_fmt and "12" in pix_fmt else 8)
    trc = v.get("color_transfer")
    hdr = "none"
    if trc == "arib-std-b67":
        hdr = "hlg"
    elif trc == "smpte2084":
        hdr = "hdr10"
    if dovi:
        hdr = "dolby_vision"
    vbr = _int(v.get("bit_rate"))
    total_bps = _int(fmt.get("bit_rate")) or (int(size * 8 / duration) if duration else None)
    abps = sum(_int(a.get("bit_rate")) or 0 for a in audio)
    if not vbr and total_bps:
        vbr = max(total_bps - abps, 0) or None
    ftags = {k.lower(): str(val) for k, val in (fmt.get("tags") or {}).items()}
    flags = []
    if "com.apple.quicktime.content.identifier" in ftags:
        flags.append("live_photo_component")
    if any("cinematic" in k for k in ftags):
        flags.append("cinematic")
    if stereo or "stereo" in (v.get("profile") or "").lower():
        flags.append("spatial")
    counts = {"subtitle": 0, "data": 0, "attachment": 0}
    for s in streams:
        t = s.get("codec_type")
        if t in counts:
            counts[t] += 1
    counts["attachment"] += sum(1 for s in streams if s.get("disposition", {}).get("attached_pic"))
    a0 = audio[0] if audio else None
    return {
        "path": str(path), "size_bytes": size, "container": (fmt.get("format_name") or "").split(",")[0],
        "duration_s": round(duration, 3) if duration else None, "bitrate_bps": total_bps,
        "probe_source": "ffprobe", "video_index": v.get("index"),
        "video": {"codec": v.get("codec_name"), "profile": v.get("profile"),
                  "width": width, "height": height,
                  "display_width": height if swapped else width, "display_height": width if swapped else height,
                  "rotation": rotation or None, "fps": round(fps, 3) if fps else None, "vfr": vfr or None,
                  "bitrate_bps": vbr, "pix_fmt": pix_fmt, "bit_depth": depth,
                  "color_transfer": trc, "color_primaries": v.get("color_primaries"),
                  "color_space": v.get("color_space"), "hdr": hdr, "dolby_vision": dovi},
        "audio": {"streams": len(audio), "codec": a0.get("codec_name") if a0 else None,
                  "channels": a0.get("channels") if a0 else None,
                  "bitrate_bps": (_int(a0.get("bit_rate")) if a0 else None)} if audio else {"streams": 0},
        "other_streams": {k: n for k, n in counts.items() if n}, "flags": flags,
    }


def probe_ffmpeg_stderr(path: Path, ffmpeg: str) -> dict:
    """Reduced-fidelity probe for ffmpeg binaries shipped without ffprobe."""
    rc, out, err = run_capture([ffmpeg, "-hide_banner", "-i", str(path)], timeout=60)
    text = err
    if "Invalid data" in text or "Duration" not in text:
        raise SkillError("unreadable", "ffmpeg could not read the file: " + text.strip()[-200:])
    size = path.stat().st_size
    dm = re.search(r"Duration:\s*(\d+):(\d+):([\d.]+)", text)
    duration = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)) if dm else None
    vl = re.search(r"Stream #0:\d+[^\n]*Video:\s*([^\n]+)", text)
    if not vl:
        raise SkillError("no_video_stream", "The file has no video stream.")
    line = vl.group(1)
    codec = line.split()[0].rstrip(",")
    prof = re.search(r"\(([^)]*)\)\s*\(", line)
    dims = re.search(r"(\d{2,5})x(\d{2,5})", line)
    width, height = (int(dims.group(1)), int(dims.group(2))) if dims else (None, None)
    fps_m = re.search(r"([\d.]+)\s*fps", line)
    br_m = re.search(r"(\d+)\s*kb/s", line)
    pix_m = re.search(r",\s*(yuv\w+|nv12|p010\w*|gbrp\w*|rgb\w*)(\([^)]*\))?", line)
    pix = pix_m.group(1) if pix_m else None
    paren = pix_m.group(2) if pix_m and pix_m.group(2) else ""
    rot_m = re.search(r"rotation of (-?[\d.]+) degrees", text)
    rotation = int(round(float(rot_m.group(1)))) if rot_m else 0
    swapped = abs(rotation) in (90, 270)
    trc = "arib-std-b67" if "arib-std-b67" in paren else "smpte2084" if "smpte2084" in paren else None
    audio_lines = re.findall(r"Stream #0:\d+[^\n]*Audio:\s*([^\n]+)", text)
    a_codec = audio_lines[0].split()[0].rstrip(",") if audio_lines else None
    a_br = re.search(r"(\d+)\s*kb/s", audio_lines[0]) if audio_lines else None
    bit = re.search(r"bitrate:\s*(\d+)\s*kb/s", text)
    total_bps = int(bit.group(1)) * 1000 if bit else (int(size * 8 / duration) if duration else None)
    vbr = int(br_m.group(1)) * 1000 if br_m else None
    flags = ["probe_reduced_fidelity"]
    return {
        "path": str(path), "size_bytes": size, "container": path.suffix.lstrip(".").lower(),
        "duration_s": round(duration, 3) if duration else None, "bitrate_bps": total_bps,
        "probe_source": "ffmpeg-stderr", "video_index": None,
        "video": {"codec": codec, "profile": prof.group(1) if prof else None, "width": width, "height": height,
                  "display_width": height if swapped else width, "display_height": width if swapped else height,
                  "rotation": rotation or None, "fps": float(fps_m.group(1)) if fps_m else None,
                  "bitrate_bps": vbr or total_bps, "pix_fmt": pix,
                  "bit_depth": 10 if pix and re.search(r"10(le|be)|p010", pix) else 8,
                  "color_transfer": trc, "hdr": "hlg" if trc == "arib-std-b67" else "hdr10" if trc else "none"},
        "audio": {"streams": len(audio_lines), "codec": a_codec,
                  "bitrate_bps": int(a_br.group(1)) * 1000 if a_br else None} if audio_lines else {"streams": 0},
        "other_streams": {}, "flags": flags,
    }


def probe_avmediainfo(path: Path) -> dict:
    rc, out, err = run_capture([AVMEDIAINFO, str(path), "--formatextensions"], timeout=60)
    if rc != 0 or "Track count" not in out:
        raise SkillError("unreadable", "avmediainfo could not read the file.")
    size = path.stat().st_size
    dur = re.search(r"Duration:\s*([\d.]+) seconds", out)
    duration = float(dur.group(1)) if dur else None
    vblock = re.search(r"Track \d+: Video.*?(?=\nTrack \d+:|\Z)", out, re.S)
    if not vblock:
        raise SkillError("no_video_stream", "The file has no video stream.")
    vb = vblock.group(0)
    fmt = re.search(r"Format:\s*([^\n']+?)\s*'", vb)
    name = (fmt.group(1) if fmt else "").lower()
    codec = "hevc" if "hevc" in name or "h.265" in name else "h264" if "h.264" in name else name.split()[0] if name else None
    dims = re.search(r"\n\s*Dimensions:\s*(\d+) x (\d+)", vb)
    width, height = (int(dims.group(1)), int(dims.group(2))) if dims else (None, None)
    mat = re.search(r"Track matrix\s*:\s*(-?[\d.]+) (-?[\d.]+)", vb)
    swapped = bool(mat and abs(float(mat.group(2))) > 0.5)
    rate = re.search(r"Estimated data rate:\s*([\d.]+) kbit/s", vb)
    fps = re.search(r"Nominal frame rate:\s*([\d.]+)", vb)
    hdr = "hlg" if re.search(r"HLG|ITU_R_2100_HLG", vb) else "hdr10" if re.search(r"SMPTE_ST_2084|PQ", vb) else "none"
    ablock = re.search(r"Track \d+: Sound.*?(?=\nTrack \d+:|\Z)", out, re.S)
    n_audio = len(re.findall(r"Track \d+: Sound", out))
    return {
        "path": str(path), "size_bytes": size, "container": path.suffix.lstrip(".").lower(),
        "duration_s": duration, "bitrate_bps": int(size * 8 / duration) if duration else None,
        "probe_source": "avmediainfo", "video_index": None,
        "video": {"codec": codec, "width": width, "height": height,
                  "display_width": height if swapped else width, "display_height": width if swapped else height,
                  "rotation": 90 if swapped else None, "fps": float(fps.group(1)) if fps else None,
                  "bitrate_bps": int(float(rate.group(1)) * 1000) if rate else None,
                  "bit_depth": 8, "hdr": hdr},
        "audio": {"streams": n_audio, "codec": "aac" if ablock and "aac" in ablock.group(0).lower() else None}
                 if n_audio else {"streams": 0},
        "other_streams": {}, "flags": ["probe_reduced_fidelity"],
    }


def probe_file(path: Path, eng: dict) -> dict:
    if not path.exists():
        raise SkillError("file_not_found", f"No such file: {path}")
    if path.is_dir():
        raise SkillError("is_directory", f"{path} is a folder; use plan or run for folders.")
    try:
        if eng.get("ffprobe"):
            return probe_ffprobe(path, eng["ffprobe"])
        if eng.get("ffmpeg"):
            return probe_ffmpeg_stderr(path, eng["ffmpeg"])
        if eng.get("avconvert"):
            return probe_avmediainfo(path)
    except SkillError as e:
        if e.code in ("unreadable", "no_video_stream") and path.suffix.lower() not in VIDEO_EXTS:
            raise SkillError("unsupported_type", f"{path.name} is not a supported video file "
                                                 f"(extension {path.suffix or 'none'}).")
        raise
    raise SkillError("no_engine", "No probe engine available.", "Run `doctor` for install hints.")


# ---------------------------------------------------------------- planning

def fps_effective(fps: float | None) -> float:
    f = min(fps or 30.0, 120.0)
    return 30.0 * (f / 30.0) ** 0.75


def output_dims(info: dict, max_dim: int | None) -> tuple[int, int, bool]:
    v = info["video"]
    w, h = v["display_width"], v["display_height"]
    scaled = False
    if max_dim and max(w, h) > max_dim:
        k = max_dim / max(w, h)
        w, h, scaled = even(w * k), even(h * k), True
    return even(w), even(h), scaled


def video_bps_of(info: dict) -> int | None:
    return info["video"].get("bitrate_bps") or info.get("bitrate_bps")


def codec_key(codec: str | None) -> str:
    return {"hevc": "hevc", "h265": "hevc", "h264": "h264", "av1": "av1", "vp9": "vp9"}.get(codec or "", "other")


def plan_density(preset: str, info: dict, compat: bool) -> dict:
    """Mirror of the app's preset rules, expressed against the skill's own density model."""
    cfg = PRESETS[preset]
    v = info["video"]
    src_bps = video_bps_of(info)
    ow, oh, scaled = output_dims(info, cfg["max_dim"])
    bpp = BASE_BPP * cfg["density_factor"] * (H264_BPP_FACTOR if compat else 1.0)
    density_bps = int(bpp * ow * oh * fps_effective(v.get("fps")))
    res = {"width": ow, "height": oh, "scaled": scaled, "density_bps": density_bps, "reason": None}
    is_hevc = codec_key(v.get("codec")) == "hevc"
    if preset == "premium" and is_hevc and src_bps and src_bps <= PREMIUM_EFFICIENT_HEVC_MAX:
        res["reason"] = "premium_efficient_hevc"
        return res
    if src_bps is not None and (src_bps <= MIN_BITRATE or src_bps * cfg["ceiling"] < MIN_BITRATE):
        res["reason"] = "below_bitrate_floor"
        return res
    if src_bps is not None:
        eff = src_bps * CODEC_EFFICIENCY.get(codec_key(v.get("codec")), DEFAULT_EFFICIENCY)
        if compat and codec_key(v.get("codec")) == "h264":
            eff = src_bps
        if eff <= density_bps and not scaled:
            res["reason"] = "already_efficient"
            return res
    target = density_bps
    if src_bps is not None:
        target = min(target, int(src_bps * cfg["ceiling"]))
    target = max(target, MIN_BITRATE)
    res["video_bps"] = target
    audio_bps = (info["audio"].get("bitrate_bps") or 128_000) if info["audio"].get("streams") else 0
    dur = info.get("duration_s") or 0
    est = int((target + audio_bps) * dur / 8) if dur else None
    res["estimated_bytes"] = est
    if est is not None and info["size_bytes"] > 0:
        saved = 1 - est / info["size_bytes"]
        if saved < MIN_MEANINGFUL_SAVING:
            res["reason"] = "high_quality_no_meaningful_savings" if preset == "high" else "no_meaningful_savings"
    return res


def plan_target(target_bytes: int, info: dict, compat: bool) -> dict:
    """Bitrate math for a size target; steps the resolution down when bits per pixel get too low."""
    dur = info.get("duration_s")
    if not dur:
        raise SkillError("no_duration", "Cannot size a target without the duration.")
    res = target_math(dur, info["video"]["display_width"], info["video"]["display_height"],
                      info["video"].get("fps"), target_bytes, compat, info["audio"].get("streams", 0) > 0)
    res["estimated_bytes"] = int(target_bytes * TARGET_SAFETY) if res["feasible"] else None
    res["reason"] = None
    if info["size_bytes"] <= target_bytes:
        res["reason"] = "already_under_target"
    elif not res["feasible"]:
        res["reason"] = "target_too_small"
    return res


def target_math(duration: float, width: int, height: int, fps: float | None, target_bytes: int,
                compat: bool, has_audio: bool) -> dict:
    usable_bits = target_bytes * TARGET_SAFETY * 8
    total_bps = usable_bits / duration
    audio_bps = 0
    if has_audio:
        audio_bps = 128_000 if total_bps > 1_200_000 else 96_000 if total_bps > 500_000 else 64_000
    video_bps = int(total_bps - audio_bps)
    longest = max(width, height)
    scale_to = longest
    factor = H264_BPP_FACTOR if compat else 1.0
    fe = fps_effective(fps)

    def bpp_at(edge: int) -> float:
        k = min(edge / longest, 1.0)
        return video_bps / (width * k * height * k * fe)

    ladder = [e for e in LADDER if e <= longest]
    if not ladder or ladder[0] != longest:
        ladder.insert(0, longest)
    for edge in ladder:
        scale_to = edge
        if bpp_at(edge) >= MIN_TARGET_BPP * factor:
            break
    k = min(scale_to / longest, 1.0)
    ow, oh = (even(width * k), even(height * k)) if k < 1 else (even(width), even(height))
    return {"video_bps": video_bps, "audio_bps": audio_bps, "width": ow, "height": oh, "scaled": k < 1,
            "bits_per_pixel": round(bpp_at(scale_to), 4), "feasible": video_bps >= MIN_VIDEO_BPS_FOR_TARGET,
            "target_bytes": target_bytes}


def choose_encoder(eng: dict, compat: bool, software: bool, hdr_or_10bit: bool) -> tuple[str | None, list[str]]:
    enc = set(eng.get("encoders", []))
    warns: list[str] = []
    if compat:
        if "libx264" in enc:
            return "libx264", warns
        if "h264_videotoolbox" in enc:
            return "h264_videotoolbox", warns
        return None, ["no_h264_encoder"]
    if not software and sys.platform == "darwin" and "hevc_videotoolbox" in enc:
        return "hevc_videotoolbox", warns
    if "libx265" in enc:
        return "libx265", warns
    if "hevc_videotoolbox" in enc and not software:
        return "hevc_videotoolbox", warns
    if "libx264" in enc and not hdr_or_10bit:
        warns.append("hevc_encoder_missing_fell_back_to_h264")
        return "libx264", warns
    return None, ["no_hevc_encoder"]


def decide(info: dict, eng: dict, opts: argparse.Namespace) -> dict:
    """Return the plan for one probed file: action compress or skip, with settings and estimates."""
    v = info["video"]
    item: dict = {"input": info["path"], "action": "skip", "original_bytes": info["size_bytes"]}
    warns: list[str] = []
    force = getattr(opts, "force", False)
    target_bytes = getattr(opts, "target_bytes", None)
    compat = bool(getattr(opts, "compat", False))
    preset = getattr(opts, "preset", None) or DEFAULT_PRESET
    hdr = v.get("hdr", "none")
    if eng["kind"] == "avconvert":
        return decide_avconvert(info, opts, item)
    # structural skips
    for flag, reason in (("live_photo_component", "live_photo_component"), ("cinematic", "format_not_preserved"),
                         ("spatial", "format_not_preserved")):
        if flag in info.get("flags", []) and not force:
            item["reason"] = reason
            return item
    if hdr == "dolby_vision" and (v.get("dolby_vision") or {}).get("profile") == 5 and not force:
        item["reason"] = "dolby_vision_profile5_needs_tonemap"
        return item
    ten_bit = (v.get("bit_depth") or 8) > 8 or hdr != "none"
    if ten_bit and getattr(opts, "hdr", "keep") == "skip" and hdr != "none":
        item["reason"] = "hdr_skipped_by_option"
        return item
    if hdr != "none" and compat:
        item["reason"] = "hdr_cannot_be_kept_with_h264_compat"
        return item
    plan = plan_target(target_bytes, info, compat) if target_bytes else plan_density(preset, info, compat)
    item["mode"] = f"target_{target_bytes // 1000000}mb" if target_bytes else preset
    if plan["reason"] and not force:
        item["reason"] = plan["reason"]
        return item
    if plan["reason"] and force:
        warns.append(f"forced_despite_{plan['reason']}")
        if "video_bps" not in plan:
            plan["video_bps"] = max(MIN_BITRATE, int((video_bps_of(info) or 4_000_000) * 0.5))
    encoder, ew = choose_encoder(eng, compat, getattr(opts, "software", False), ten_bit)
    warns += ew
    if not encoder:
        item["reason"] = ew[0] if ew else "no_encoder"
        return item
    if encoder in ("libx264", "h264_videotoolbox") and ten_bit and not compat:
        item["reason"] = "hdr_cannot_be_kept_with_h264_fallback"
        return item
    if hdr == "dolby_vision":
        warns.append("dolby_vision_metadata_dropped_base_layer_kept")
    if v.get("vfr"):
        warns.append("variable_frame_rate_source")
    if info["other_streams"].get("data") or info["other_streams"].get("subtitle"):
        warns.append("data_and_subtitle_streams_not_copied")
    if eng["kind"] == "imageio":
        if ((info.get("duration_s") or 0) > SANDBOX_MAX_SECONDS or info["size_bytes"] > SANDBOX_MAX_BYTES) \
                and not getattr(opts, "ignore_sandbox_limits", False):
            item["reason"] = "too_large_for_sandbox"
            item["hint"] = (f"Limit: {SANDBOX_MAX_SECONDS}s and {human_size(SANDBOX_MAX_BYTES)} for this engine. "
                            "Use `advise` for a command to run elsewhere, or pass --ignore-sandbox-limits.")
            return item
    audio_plan = plan_audio(info, plan, compat, bool(target_bytes))
    item.update({
        "action": "compress",
        "settings": {"codec": "h264" if encoder in ("libx264", "h264_videotoolbox") else "hevc",
                     "encoder": encoder, "video_kbps": plan["video_bps"] // 1000,
                     "resolution": f"{plan['width']}x{plan['height']}" if plan.get("scaled") else "keep",
                     "audio": audio_plan["label"], "ten_bit": ten_bit or None},
        "estimated_bytes": plan.get("estimated_bytes"),
        "estimated_saved_percent": round(100 * (1 - plan["estimated_bytes"] / info["size_bytes"]), 1)
        if plan.get("estimated_bytes") and info["size_bytes"] else None,
        "warnings": warns or None,
        "_internal": {"plan": plan, "encoder": encoder, "audio": audio_plan, "target_bytes": target_bytes,
                      "preset": preset, "ten_bit": ten_bit},
    })
    return item


def plan_audio(info: dict, plan: dict, compat: bool, target_mode: bool) -> dict:
    a = info["audio"]
    if not a.get("streams"):
        return {"label": "none", "args": [], "copy": False}
    codec = a.get("codec")
    src_bps = a.get("bitrate_bps")
    if target_mode:
        want = plan.get("audio_bps") or 96_000
        if codec == "aac" and src_bps and src_bps <= want * 1.15:
            return {"label": "copy", "args": ["-c:a", "copy"], "copy": True}
        return {"label": f"aac {want // 1000}k", "args": ["-c:a", "aac", "-b:a", str(want)], "copy": False}
    if codec in COPYABLE_AUDIO and not (compat and codec != "aac"):
        return {"label": "copy", "args": ["-c:a", "copy"], "copy": True}
    return {"label": "aac 160k", "args": ["-c:a", "aac", "-b:a", "160000"], "copy": False}


def decide_avconvert(info: dict, opts: argparse.Namespace, item: dict) -> dict:
    preset = getattr(opts, "preset", None) or DEFAULT_PRESET
    if getattr(opts, "target_bytes", None):
        item["reason"] = "engine_cannot_target_size"
        item["hint"] = "avconvert has fixed presets. Install ffmpeg (see `doctor`) or use `advise`."
        return item
    if getattr(opts, "compat", False):
        item["reason"] = "engine_cannot_force_h264_compat"
        return item
    if info["video"].get("hdr", "none") != "none" and not getattr(opts, "force", False):
        item["reason"] = "hdr_unsupported_on_avconvert_path"
        return item
    plan = plan_density(preset, info, False)
    item["mode"] = preset
    if plan["reason"] and not getattr(opts, "force", False):
        item["reason"] = plan["reason"]
        return item
    item.update({"action": "compress",
                 "settings": {"codec": "h264" if preset == "max-saving" else "hevc", "encoder": "avconvert",
                              "avconvert_preset": AVCONVERT_PRESETS[preset], "resolution": "preset-defined"},
                 "estimated_bytes": plan.get("estimated_bytes"),
                 "warnings": ["avconvert_fixed_presets_size_is_not_controllable", "avconvert_strips_private_metadata"],
                 "_internal": {"plan": plan, "encoder": "avconvert", "preset": preset}})
    return item


def public_item(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "_internal"}


# ---------------------------------------------------------------- encoding

def out_path_for(src: Path, out_dir: Path | None) -> Path:
    base = src.stem + OUTPUT_SUFFIX
    return (out_dir or src.parent) / base


def build_encode_cmd(eng: dict, info: dict, internal: dict, src: str, out: str, *,
                     video_bps: int, encoder: str, pass_no: int = 0, passlog: str = "pass") -> list[str]:
    v = info["video"]
    plan = internal["plan"]
    cmd = [eng["ffmpeg"], "-hide_banner", "-nostdin", "-n", "-i", src]
    vidx = info.get("video_index")
    cmd += ["-map", f"0:{vidx}" if vidx is not None else "0:v:0"]
    if pass_no != 1:
        cmd += ["-map", "0:a?"]
    cmd += ["-map_metadata", "0"]
    threads = os.environ.get("VIDEO_COMPRESSION_THREADS", "").strip()
    if threads.isdigit() and int(threads) > 0:
        cmd += ["-threads", threads]
    if plan.get("scaled"):
        cmd += ["-vf", f"scale={plan['width']}:{plan['height']}:flags=lanczos"]
    ten = internal.get("ten_bit")
    if encoder == "hevc_videotoolbox":
        cmd += ["-c:v", encoder, "-b:v", str(video_bps), "-tag:v", "hvc1", "-allow_sw", "1"]
        cmd += ["-pix_fmt", "p010le", "-profile:v", "main10"] if ten else ["-pix_fmt", "yuv420p"]
    elif encoder == "libx265":
        speed = "fast" if eng["kind"] == "imageio" else "medium"
        cmd += ["-c:v", encoder, "-preset", speed, "-b:v", str(video_bps), "-tag:v", "hvc1"]
        cmd += ["-pix_fmt", "yuv420p10le", "-profile:v", "main10"] if ten else ["-pix_fmt", "yuv420p"]
        xp = "log-level=error"
        if pass_no:
            xp += f":pass={pass_no}:stats={passlog}.x265"
        cmd += ["-x265-params", xp]
    elif encoder == "libx264":
        speed = "veryfast" if eng["kind"] == "imageio" else "medium"
        cmd += ["-c:v", encoder, "-preset", speed, "-b:v", str(video_bps), "-pix_fmt", "yuv420p",
                "-profile:v", "high"]
        if pass_no:
            cmd += ["-pass", str(pass_no), "-passlogfile", passlog]
    else:  # h264_videotoolbox
        cmd += ["-c:v", encoder, "-b:v", str(video_bps), "-pix_fmt", "yuv420p", "-profile:v", "high"]
    for opt, key in (("-color_primaries", "color_primaries"), ("-color_trc", "color_transfer"),
                     ("-colorspace", "color_space")):
        val = v.get(key)
        if val and val != "unknown":
            cmd += [opt, val]
    if pass_no == 1:
        cmd += ["-progress", "pipe:1", "-nostats", "-an", "-f", "null", os.devnull]
        cmd[cmd.index("-n")] = "-y"
    else:
        cmd += internal["audio"]["args"] if internal.get("audio") else []
        cmd += ["-movflags", "+faststart+use_metadata_tags", "-progress", "pipe:1", "-nostats", out]
    return cmd


def run_ffmpeg(cmd: list[str], duration: float | None, label: str, cwd: str | None = None,
               timeout: int | None = None) -> tuple[int, str]:
    err_file = tempfile.TemporaryFile(mode="w+", errors="replace")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err_file, text=True, cwd=cwd,
                            stdin=subprocess.DEVNULL, errors="replace")
    timer = None
    if timeout:
        timer = threading.Timer(timeout, proc.kill)
        timer.start()
    last_pct = -10
    try:
        for line in proc.stdout or []:
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    t = int(line.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                if duration:
                    pct = min(99, int(100 * t / duration))
                    if pct >= last_pct + 10:
                        last_pct = pct - pct % 10
                        log(f"[{label}] {pct}%")
        proc.wait()
    except BaseException:
        proc.kill()  # never leave an ffmpeg process behind on interrupt, SIGTERM or error
        proc.wait()
        raise
    finally:
        if timer:
            timer.cancel()
    err_file.seek(0)
    tail = err_file.read()[-600:]
    err_file.close()
    return proc.returncode, tail


def safe_remove(path: str | Path | None) -> None:
    """Remove one file this process created. Never globs, never touches originals."""
    if not path:
        return
    p = Path(path)
    if p.name.endswith(".partial.mp4") or p.name.endswith(".partial"):
        try:
            p.unlink()
        except FileNotFoundError:
            pass


def encode_one(eng: dict, info: dict, item: dict, out_final: Path, opts: argparse.Namespace) -> dict:
    internal = item["_internal"]
    src = info["path"]
    partial = out_final.with_name(f"{out_final.stem}.{os.getpid()}.partial.mp4")
    timeout = getattr(opts, "timeout", None)
    started = time.time()
    if eng["kind"] == "avconvert":
        return encode_avconvert(eng, info, internal, out_final, partial, started, timeout)
    encoder = internal["encoder"]
    plan = internal["plan"]
    target_bytes = internal.get("target_bytes")
    video_bps = plan["video_bps"]
    duration = info.get("duration_s")
    tmpdir = tempfile.mkdtemp(prefix="video-compression-")
    attempts: list[str] = []
    result_note: dict = {}
    try:
        for attempt in range(3):
            safe_remove(partial)
            two_pass = bool(target_bytes) and encoder in ("libx264", "libx265")
            rc = 0
            tail = ""
            if two_pass:
                cmd1 = build_encode_cmd(eng, info, internal, src, str(partial), video_bps=video_bps,
                                        encoder=encoder, pass_no=1)
                rc, tail = run_ffmpeg(cmd1, duration, Path(src).name + " pass 1", cwd=tmpdir, timeout=timeout)
            if rc == 0:
                cmd = build_encode_cmd(eng, info, internal, src, str(partial), video_bps=video_bps,
                                       encoder=encoder, pass_no=2 if two_pass else 0)
                rc, tail = run_ffmpeg(cmd, duration, Path(src).name, cwd=tmpdir, timeout=timeout)
            if rc != 0:
                safe_remove(partial)
                attempts.append(f"{encoder}: exit {rc}: {tail.strip().splitlines()[-1] if tail.strip() else ''}")
                if encoder == "hevc_videotoolbox" and "libx265" in eng.get("encoders", []):
                    encoder = "libx265"
                    result_note["encoder_fallback"] = "hevc_videotoolbox_failed_used_libx265"
                    continue
                return {"status": "failed", "reason": "encoder_failed", "detail": attempts[-1],
                        "seconds": round(time.time() - started, 1)}
            if target_bytes and partial.exists() and partial.stat().st_size > target_bytes and attempt < 2:
                ratio = target_bytes * TARGET_SAFETY / partial.stat().st_size
                video_bps = max(MIN_VIDEO_BPS_FOR_TARGET, int(video_bps * ratio * 0.97))
                result_note["target_retries"] = result_note.get("target_retries", 0) + 1
                continue
            break
        return finalize(eng, info, partial, out_final, started, encoder, result_note, internal)
    finally:
        cleanup_tmpdir(tmpdir)
        safe_remove(partial)


def encode_avconvert(eng: dict, info: dict, internal: dict, out_final: Path, partial: Path,
                     started: float, timeout: int | None) -> dict:
    preset = AVCONVERT_PRESETS[internal["preset"]]
    cmd = [AVCONVERT, "--source", info["path"], "--output", str(partial), "--preset", preset]
    rc, out, err = run_capture(cmd, timeout=timeout or 3600)
    if rc != 0 or not partial.exists():
        safe_remove(partial)
        return {"status": "failed", "reason": "encoder_failed", "detail": (err or out).strip()[-200:],
                "seconds": round(time.time() - started, 1)}
    try:
        return finalize(eng, info, partial, out_final, started, "avconvert", {}, internal)
    finally:
        safe_remove(partial)


def cleanup_tmpdir(tmpdir: str) -> None:
    try:
        for name in os.listdir(tmpdir):
            try:
                os.unlink(os.path.join(tmpdir, name))
            except OSError:
                pass
        os.rmdir(tmpdir)
    except OSError:
        pass


def finalize(eng: dict, info: dict, partial: Path, out_final: Path, started: float, encoder: str,
             notes: dict, internal: dict) -> dict:
    """Verify the partial output, enforce the not-smaller gates, then move it into place."""
    seconds = round(time.time() - started, 1)
    if not partial.exists():
        return {"status": "failed", "reason": "no_output", "seconds": seconds}
    try:
        out_info = probe_file(partial, eng)
    except SkillError as e:
        safe_remove(partial)
        return {"status": "failed", "reason": "integrity_check_failed", "detail": e.message, "seconds": seconds}
    expect = internal["plan"]
    report = verify_pair(info, out_info, expect_dims=(expect["width"], expect["height"])
                         if expect.get("scaled") and encoder != "avconvert" else None)
    orig, new = info["size_bytes"], out_info["size_bytes"]
    max_share = PRESETS.get(internal.get("preset"), {}).get("max_share", 1.0) if not internal.get("target_bytes") else 1.0
    failed_checks = [c for c, r in report["checks"].items() if not r["ok"] and c != "smaller"]
    base = {"original_bytes": orig, "output_bytes": new, "saved_bytes": orig - new,
            "saved_percent": round(100 * (orig - new) / orig, 1) if orig else None,
            "encoder": encoder, "seconds": seconds}
    base.update(notes)
    if failed_checks:
        safe_remove(partial)
        return {"status": "failed", "reason": "integrity_check_failed", "verify": report, **base}
    tb = internal.get("target_bytes")
    if tb and new > tb:
        safe_remove(partial)
        return {"status": "skipped", "reason": "could_not_reach_target", "verify": report, **base}
    if not tb and new > orig * max_share:
        safe_remove(partial)
        return {"status": "skipped", "reason": "not_smaller", "verify": report, **base}
    if not tb and (orig - new) / orig < MIN_MEANINGFUL_SAVING and internal.get("preset") == "high":
        safe_remove(partial)
        return {"status": "skipped", "reason": "high_quality_no_meaningful_savings", "verify": report, **base}
    if out_final.exists():
        safe_remove(partial)
        return {"status": "skipped", "reason": "output_exists", "output": str(out_final), **base}
    os.replace(partial, out_final)
    return {"status": "compressed", "output": str(out_final), "verify": {"ok": report["ok"]}, **base}


# ---------------------------------------------------------------- verification

def verify_pair(orig: dict, out: dict, expect_dims: tuple[int, int] | None = None) -> dict:
    checks: dict = {}

    def add(name: str, ok: bool, detail: str):
        checks[name] = {"ok": bool(ok), "detail": detail}

    od, nd = orig.get("duration_s"), out.get("duration_s")
    if od and nd:
        tol = max(0.5, 0.01 * od)
        add("duration", abs(od - nd) <= tol, f"original {od:.2f}s, output {nd:.2f}s, tolerance {tol:.2f}s")
    else:
        add("duration", False, "duration missing")
    oa, na = orig["audio"].get("streams", 0), out["audio"].get("streams", 0)
    add("audio_streams", oa == na, f"original {oa}, output {na}")
    add("video_present", bool(out["video"].get("codec")), f"codec {out['video'].get('codec')}")
    of, nf = orig["video"].get("fps"), out["video"].get("fps")
    if of and nf:
        tol = 0.05 if orig["video"].get("vfr") else 0.02
        add("fps", abs(of - nf) / of <= tol, f"original {of}, output {nf}")
    ow, oh = orig["video"].get("display_width"), orig["video"].get("display_height")
    nw, nh = out["video"].get("display_width"), out["video"].get("display_height")
    if expect_dims and nw and nh:
        add("resolution", (nw, nh) == tuple(expect_dims) or (nh, nw) == tuple(expect_dims),
            f"expected {expect_dims[0]}x{expect_dims[1]}, got {nw}x{nh}")
    elif ow and oh and nw and nh:
        ok = abs((ow / oh) - (nw / nh)) / (ow / oh) <= 0.02 and nw <= ow + 2 and nh <= oh + 2
        add("resolution", ok, f"original {ow}x{oh}, output {nw}x{nh}")
    saved = orig["size_bytes"] - out["size_bytes"]
    add("smaller", saved > 0, f"original {orig['size_bytes']} B, output {out['size_bytes']} B")
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks, "saved_bytes": saved,
            "saved_percent": round(100 * saved / orig["size_bytes"], 1) if orig["size_bytes"] else None}


# ---------------------------------------------------------------- commands

def collect_inputs(path: Path, recursive: bool) -> list[Path]:
    if not path.exists():
        raise SkillError("file_not_found", f"No such file or folder: {path}")
    if path.is_file():
        return [path]
    it = path.rglob("*") if recursive else path.iterdir()
    files = [p for p in it if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in VIDEO_EXTS
             and not p.name.endswith(OUTPUT_SUFFIX) and ".partial" not in p.name]
    return sorted(files)


def make_opts_target(args: argparse.Namespace) -> None:
    tm = getattr(args, "target_mb", None)
    tg = getattr(args, "target", None)
    if tm and tg:
        raise SkillError("bad_arguments", "Use either --target-mb or --target, not both.")
    if tg:
        if tg not in PLATFORM_LIMITS_MB:
            raise SkillError("unknown_target", f"Unknown target '{tg}'.",
                             "Known targets: " + ", ".join(f"{k} ({v} MB)" for k, v in PLATFORM_LIMITS_MB.items()))
        tm = PLATFORM_LIMITS_MB[tg]
    if tm is not None and tm <= 0:
        raise SkillError("bad_arguments", "--target-mb must be greater than 0.")
    args.target_bytes = int(tm * 1_000_000) if tm else None


def plan_items(args: argparse.Namespace, eng: dict) -> tuple[list[dict], list[dict]]:
    src = Path(os.path.abspath(os.path.expanduser(args.path)))
    files = collect_inputs(src, getattr(args, "recursive", False))
    if src.is_dir() and not files:
        raise SkillError("no_videos", f"No video files found in {src}.",
                         "Use --recursive to include subfolders.")
    out_dir = Path(os.path.abspath(os.path.expanduser(args.out_dir))) if getattr(args, "out_dir", None) else None
    items, infos = [], []
    for f in files:
        try:
            info = probe_file(f, eng)
            item = decide(info, eng, args)
            outp = out_path_for(f, out_dir)
            item["output"] = str(outp)
            if item["action"] == "compress" and outp.exists():
                item["action"], item["reason"] = "skip", "output_exists"
        except SkillError as e:
            info = None
            item = {"input": str(f), "action": "error", "error": {"code": e.code, "message": e.message}}
        items.append(item)
        infos.append(info)
    if len(items) == 1 and items[0]["action"] == "error" and not src.is_dir():
        e = items[0]["error"]
        raise SkillError(e["code"], e["message"])
    return items, infos


def cmd_doctor(args) -> dict:
    eng = detect_engines()
    res = {"ok": True, "script_version": SCRIPT_VERSION, "os": platform.system(), "os_release": platform.release(),
           "arch": platform.machine(), "python": platform.python_version(),
           "tier": eng["tier"], "engine": eng["kind"]}
    labels = {"1": "local executor with ffmpeg and ffprobe", "1b": "macOS avconvert only (fixed presets)",
              "2": "ffmpeg binary without ffprobe (hosted sandbox or Python package)",
              "3": "advisor only, no engine"}
    res["tier_label"] = labels[eng["tier"]]
    engines: dict = {}
    if eng.get("ffmpeg"):
        e = _finish_engine(dict(eng))
        engines["ffmpeg"] = {"path": eng["ffmpeg"], "version": e["ffmpeg_version"], "encoders": e["encoders"],
                             "ffprobe": eng.get("ffprobe")}
    elif eng.get("system_ffmpeg"):
        engines["ffmpeg"] = {"path": eng["system_ffmpeg"]}
    if eng.get("avconvert"):
        engines["avconvert"] = {"path": eng["avconvert"], "note": "fixed presets, no exact size targeting"}
    res["engines"] = engines
    res["can_run"] = eng["kind"] != "none"
    res["can_target_size"] = eng["kind"] in ("ffmpeg", "imageio")
    res["app_tip_enabled"] = os.environ.get("VIDEO_COMPRESSION_SKILL_TIP", "1") != "0"
    if eng["kind"] in ("none", "avconvert", "imageio") or not eng.get("ffprobe"):
        res["install_hints"] = install_hints()
    res["network"] = "never used unless --allow-download is passed"
    return res


def cmd_probe(args) -> dict:
    eng = resolve_engine(args.allow_download)
    info = probe_file(Path(os.path.abspath(os.path.expanduser(args.path))), eng)
    info["ok"] = True
    return info


def cmd_plan(args) -> dict:
    make_opts_target(args)
    eng = resolve_engine(args.allow_download)
    items, _ = plan_items(args, eng)
    return plan_result(args, eng, items)


def plan_result(args, eng: dict, items: list[dict]) -> dict:
    comp = [i for i in items if i["action"] == "compress"]
    est = sum(i["original_bytes"] - (i.get("estimated_bytes") or i["original_bytes"]) for i in comp)
    res = {"ok": True, "engine": eng["kind"], "tier": eng["tier"],
           "mode": f"target_{args.target_bytes // 1000000}mb" if args.target_bytes else args.preset,
           "summary": {"files": len(items), "compress": len(comp),
                       "skip": sum(1 for i in items if i["action"] == "skip"),
                       "error": sum(1 for i in items if i["action"] == "error"),
                       "estimated_saved_bytes": est},
           "items": [public_item(i) for i in items]}
    return res


def cmd_run(args) -> dict:
    make_opts_target(args)
    eng = resolve_engine(args.allow_download)
    items, infos = plan_items(args, eng)
    if args.dry_run:
        res = plan_result(args, eng, items)
        res["dry_run"] = True
        return res
    if args.out_dir:
        Path(os.path.abspath(os.path.expanduser(args.out_dir))).mkdir(parents=True, exist_ok=True)
    results = []
    for item, info in zip(items, infos):
        entry = {"input": item["input"]}
        if item["action"] == "error":
            entry.update({"status": "failed", "reason": item["error"]["code"], "detail": item["error"]["message"]})
        elif item["action"] == "skip":
            entry.update({"status": "skipped", "reason": item.get("reason"), "hint": item.get("hint"),
                          "original_bytes": item["original_bytes"]})
        else:
            try:
                r = encode_one(eng, info, item, Path(item["output"]), args)
            except KeyboardInterrupt:
                raise
            except Exception as e:  # keep the batch going
                r = {"status": "failed", "reason": "unexpected_error", "detail": f"{type(e).__name__}: {e}"}
            entry.update(r)
            if item.get("warnings"):
                entry["warnings"] = item["warnings"]
        results.append(entry)
    ok_n = sum(1 for r in results if r["status"] == "compressed")
    saved = sum(r.get("saved_bytes", 0) for r in results if r["status"] == "compressed")
    orig = sum(r.get("original_bytes", 0) for r in results if r["status"] == "compressed")
    return {"ok": not any(r["status"] == "failed" for r in results), "engine": eng["kind"], "tier": eng["tier"],
            "mode": f"target_{args.target_bytes // 1000000}mb" if args.target_bytes else args.preset,
            "summary": {"files": len(results), "compressed": ok_n,
                        "skipped": sum(1 for r in results if r["status"] == "skipped"),
                        "failed": sum(1 for r in results if r["status"] == "failed"),
                        "saved_bytes": saved,
                        "saved_percent": round(100 * saved / orig, 1) if orig else None,
                        "originals_untouched": True},
            "results": results}


def cmd_verify(args) -> dict:
    eng = resolve_engine(args.allow_download)
    o = probe_file(Path(os.path.abspath(os.path.expanduser(args.original))), eng)
    n = probe_file(Path(os.path.abspath(os.path.expanduser(args.output))), eng)
    dims = (args.expect_width, args.expect_height) if args.expect_width and args.expect_height else None
    rep = verify_pair(o, n, dims)
    rep["original_bytes"], rep["output_bytes"] = o["size_bytes"], n["size_bytes"]
    if "probe_reduced_fidelity" in o.get("flags", []) + n.get("flags", []):
        rep["note"] = "Reduced-fidelity probe (no ffprobe): fps and stream checks use ffmpeg or avmediainfo output."
    return rep


def advise_commands(width: int, height: int, video_bps: int, audio_bps: int, scale_to: tuple[int, int] | None,
                    compat: bool) -> dict:
    vk, ak = max(video_bps // 1000, 1), audio_bps // 1000
    vf = f' -vf scale={scale_to[0]}:{scale_to[1]}' if scale_to else ""
    if compat:
        venc, extra = "libx264", ""
    else:
        venc, extra = "libx265", " -tag:v hvc1"
    audio = f" -c:a aac -b:a {ak}k" if audio_bps else " -an"
    passopt = "-pass {n} -passlogfile vcpass" if compat else "-x265-params pass={n}:stats=vcpass.x265"
    p1 = f'ffmpeg -y -i "INPUT.mp4" -c:v {venc} -b:v {vk}k{vf}{extra} {passopt.format(n=1)} -an -f null '
    p2 = (f'ffmpeg -i "INPUT.mp4" -c:v {venc} -b:v {vk}k{vf}{extra} {passopt.format(n=2)}{audio} '
          f'-movflags +faststart "OUTPUT.mp4"')
    unix = p1 + "/dev/null && " + p2
    windows = p1 + "NUL && " + p2
    cmds = {"linux": unix, "macos": unix, "windows": windows,
            "cleanup_note": ("Delete the pass logs afterwards by name: "
                             + ("vcpass-0.log and vcpass-0.log.mbtree" if compat else "vcpass.x265 and vcpass.x265.cutree")),
            "replace": "Replace INPUT.mp4 and OUTPUT.mp4 with your file names; keep the quotes if a path contains spaces."}
    cmds["macos_no_ffmpeg"] = ('/usr/bin/avconvert --source "INPUT.mov" --output "OUTPUT.mp4" '
                               "--preset PresetHEVC1920x1080  (fixed preset; the size is not controllable)")
    return cmds


def cmd_advise(args) -> dict:
    make_opts_target(args)
    if not args.target_bytes:
        raise SkillError("bad_arguments", "advise needs --target-mb N or --target <name>.",
                         "Known targets: " + ", ".join(f"{k} ({v} MB)" for k, v in PLATFORM_LIMITS_MB.items()))
    if args.duration <= 0:
        raise SkillError("bad_arguments", "--duration must be greater than 0 seconds.")
    assumed = []
    if not args.width or not args.height:
        assumed.append("resolution 1920x1080")
    w, h = args.width or 1920, args.height or 1080
    m = target_math(args.duration, w, h, args.fps, args.target_bytes, args.compat, not args.no_audio)
    if not m["feasible"]:
        return {"ok": True, "feasible": False, "target_mb": args.target_bytes / 1e6,
                "video_kbps": max(m["video_bps"] // 1000, 0),
                "message": "The target leaves almost no bitrate for video. Trim the clip, lower the "
                           "resolution further, or choose a larger target."}
    scale_to = (m["width"], m["height"]) if m["scaled"] else None
    # portrait-aware scale: keep the original aspect ratio, even dimensions
    cmds = advise_commands(w, h, m["video_bps"], m["audio_bps"], scale_to, args.compat)
    return {"ok": True, "feasible": True, "target_mb": args.target_bytes / 1e6, "safety_margin": TARGET_SAFETY,
            "codec": "h264" if args.compat else "hevc", "video_kbps": m["video_bps"] // 1000,
            "audio_kbps": m["audio_bps"] // 1000, "resolution": f"{m['width']}x{m['height']}",
            "bits_per_pixel": m["bits_per_pixel"], "assumed": assumed or None, "commands": cmds,
            "note": ("Chat apps and older devices play H.264 most reliably; use --compat for it. "
                     "HEVC output carries the hvc1 tag so Apple devices play it." )}


# ---------------------------------------------------------------- human output

def render_human(cmd: str, res: dict) -> str:
    L: list[str] = []
    if not res.get("ok", True) and "error" in res:
        e = res["error"]
        return f"Error ({e['code']}): {e['message']}" + (f"\nHint: {e['hint']}" if e.get("hint") else "")
    if cmd == "doctor":
        L.append(f"Tier {res['tier']}: {res['tier_label']}")
        L.append(f"OS: {res['os']} {res['arch']}, Python {res['python']}")
        for name, e in res.get("engines", {}).items():
            L.append(f"Engine {name}: {e.get('path')}" + (f" (version {e['version']})" if e.get("version") else ""))
            if e.get("encoders"):
                L.append("  encoders: " + ", ".join(e["encoders"]))
        L.append(f"Can run: {res['can_run']}; can target a size: {res['can_target_size']}")
        for k, v in res.get("install_hints", {}).items():
            L.append(f"Hint ({k}): {v}")
    elif cmd == "probe":
        v, a = res["video"], res["audio"]
        L.append(f"{res['path']}")
        L.append(f"Size {human_size(res['size_bytes'])}, duration {res.get('duration_s')}s, container {res['container']}")
        L.append(f"Video {v.get('codec')} {v.get('display_width')}x{v.get('display_height')} "
                 f"{v.get('fps')} fps, {human_size((v.get('bitrate_bps') or 0) / 8)}/s, {v.get('bit_depth')}-bit, HDR {v.get('hdr')}")
        if v.get("rotation"):
            L.append(f"Rotation {v['rotation']} degrees")
        L.append(f"Audio {a.get('codec')} x{a.get('streams')}" if a.get("streams") else "Audio none")
        if res.get("flags"):
            L.append("Flags: " + ", ".join(res["flags"]))
    elif cmd in ("plan", "run"):
        s = res["summary"]
        L.append(f"Engine {res['engine']} (tier {res['tier']}), mode {res['mode']}")
        key = "results" if cmd == "run" and "results" in res else "items"
        for it in res[key]:
            name = Path(it["input"]).name
            if "status" in it:
                st = it["status"]
                if st == "compressed":
                    L.append(f"{name}: compressed, saved {human_size(it['saved_bytes'])} ({it['saved_percent']}%) "
                             f"with {it['encoder']} in {it['seconds']}s -> {Path(it['output']).name}")
                else:
                    L.append(f"{name}: {st} ({it.get('reason')})" + (f" {it['detail']}" if it.get("detail") else ""))
            else:
                if it["action"] == "compress":
                    L.append(f"{name}: compress with {it['settings']['encoder']} at {it['settings']['video_kbps']} kbps"
                             f", estimated saving {it.get('estimated_saved_percent')}%")
                else:
                    L.append(f"{name}: {it['action']} ({it.get('reason') or it.get('error', {}).get('message')})")
        L.append("Summary: " + ", ".join(f"{k} {v}" for k, v in s.items()))
    elif cmd == "verify":
        for name, c in res["checks"].items():
            L.append(f"{'PASS' if c['ok'] else 'FAIL'} {name}: {c['detail']}")
        L.append(f"Saved {human_size(res['saved_bytes'])} ({res['saved_percent']}%). Overall: {'OK' if res['ok'] else 'FAILED'}")
    elif cmd == "advise":
        if not res.get("feasible"):
            L.append(res["message"])
        else:
            L.append(f"Target {res['target_mb']:.0f} MB: {res['codec']} {res['video_kbps']} kbps video, "
                     f"{res['audio_kbps']} kbps audio, {res['resolution']}")
            for k in ("macos", "linux", "windows"):
                L.append(f"{k}: {res['commands'][k]}")
            L.append(res["commands"]["cleanup_note"])
    return "\n".join(L)


# ---------------------------------------------------------------- CLI

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="video_compress.py", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, paths=True):
        p.add_argument("--human", action="store_true", help="readable text instead of compact JSON")
        p.add_argument("--allow-download", action="store_true",
                       help="allow a user-space ffmpeg download via pip (network) when no engine is found")

    def encoding(p):
        p.add_argument("path", help="video file or folder")
        p.add_argument("--preset", choices=list(PRESETS), default=DEFAULT_PRESET)
        p.add_argument("--target-mb", type=float, help="fit under this many megabytes (1 MB = 1,000,000 bytes)")
        p.add_argument("--target", help="platform limit name: " + ", ".join(PLATFORM_LIMITS_MB))
        p.add_argument("--compat", action="store_true", help="H.264 output for maximum compatibility")
        p.add_argument("--software", action="store_true", help="use libx265 instead of hevc_videotoolbox")
        p.add_argument("--hdr", choices=["keep", "skip"], default="keep")
        p.add_argument("--recursive", action="store_true", help="include subfolders when path is a folder")
        p.add_argument("--out-dir", help="write outputs here instead of next to each source")
        p.add_argument("--force", action="store_true", help="encode even when the plan says skip (gates still apply)")
        p.add_argument("--ignore-sandbox-limits", action="store_true")
        p.add_argument("--timeout", type=int, help="seconds per ffmpeg process")

    p = sub.add_parser("doctor", help="detect tier, engines, install hints")
    common(p)
    p = sub.add_parser("probe", help="inspect one file")
    p.add_argument("path")
    common(p)
    p = sub.add_parser("plan", help="decide settings and skips, no encoding")
    encoding(p)
    common(p)
    p = sub.add_parser("run", help="compress, verify, keep originals")
    encoding(p)
    p.add_argument("--dry-run", action="store_true", help="same as plan")
    common(p)
    p = sub.add_parser("verify", help="check an output against its original")
    p.add_argument("original")
    p.add_argument("output")
    p.add_argument("--expect-width", type=int)
    p.add_argument("--expect-height", type=int)
    common(p)
    p = sub.add_parser("advise", help="bitrate math and commands without executing anything")
    p.add_argument("--duration", type=float, required=True, help="clip length in seconds")
    p.add_argument("--width", type=int)
    p.add_argument("--height", type=int)
    p.add_argument("--fps", type=float, default=30.0)
    p.add_argument("--target-mb", type=float)
    p.add_argument("--target", help="platform limit name: " + ", ".join(PLATFORM_LIMITS_MB))
    p.add_argument("--compat", action="store_true", help="H.264 output")
    p.add_argument("--no-audio", action="store_true")
    common(p)
    return ap


COMMANDS = {"doctor": cmd_doctor, "probe": cmd_probe, "plan": cmd_plan, "run": cmd_run,
            "verify": cmd_verify, "advise": cmd_advise}


def _raise_interrupt(signum, frame):
    raise KeyboardInterrupt


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        signal.signal(signal.SIGTERM, _raise_interrupt)  # SIGTERM stops the encode and cleans up like Ctrl-C
    except (ValueError, OSError):
        pass  # not the main thread, or not supported on this platform
    try:
        res = COMMANDS[args.cmd](args)
        code = 0 if res.get("ok", True) else 1
    except SkillError as e:
        res = {"ok": False, "error": {"code": e.code, "message": e.message, "hint": e.hint}}
        code = 1
    except KeyboardInterrupt:
        res = {"ok": False, "error": {"code": "interrupted", "message": "Interrupted; originals untouched."}}
        code = 130
    res = compact(res)
    if args.human:
        print(render_human(args.cmd, res))
    else:
        print(json.dumps(res, separators=(",", ":"), ensure_ascii=False))
    return code


if __name__ == "__main__":
    sys.exit(main())
