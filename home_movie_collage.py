#!/usr/bin/env python3
"""Build a random, heavily-zoomed, silent video collage from a folder (or folders) of videos.

Pipeline:
  1. discover video files under the given paths (skipping iCloud placeholders and earlier collages),
  2. ffprobe each one (cached in ~/Library/Caches/home-movie-collage on macOS,
     ~/.cache/home-movie-collage on Linux, or under LOCALAPPDATA on Windows),
  3. plan random clips in whole frames: source, start, length, speed, rotation, fixed zoom window,
     transition,
  4. render every clip to a normalized intermediate in parallel, dropping any that fail,
  5. join intermediates with hard cuts or crossfades into the final file.

The plan is written next to the output as <output>.plan.json so you can trace any shot back
to its source; re-running with the same --seed and sources reproduces the same plan.
"""

import argparse
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import metadata
from pathlib import Path

try:
    import resource  # Unix only: lets assemble() lift the open-file limit for long collages
except ImportError:
    resource = None

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".3gp"}
SF_DATALESS = 0x40000000  # macOS st_flags bit for iCloud files whose data is not on disk
MIN_FFMPEG = 7  # assemble() reads its filtergraph with the -/filter_complex syntax ffmpeg 7.0 introduced
FFMPEG_HINT = ("on macOS run `brew install ffmpeg`, on Windows `winget install Gyan.FFmpeg`; "
               "for Linux see https://github.com/williamthazard/home-movie-collage#install")
PROBE_VERSION = 2  # bump when probe() records new fields, so cached entries get probed again
HDR_TRANSFERS = {"arib-std-b67": "hlg", "smpte2084": "pq"}  # ffprobe color_transfer -> plan name
SIZE_PRESETS = {"landscape": (1280, 720), "square": (720, 720), "portrait": (720, 1280)}  # --size names
# Random ranges that --clip, --zoom and --fade can pin to one value: name -> default (min, max).
RANGES = {"clip": (1.5, 6.0), "zoom": (2.5, 7.0), "fade": (0.4, 2.5)}
H264 = ["-c:v", "libx264", "-preset", "medium", "-crf", "18"]
FASTSTART = ["-movflags", "+faststart"]  # index at the front, so playback starts before the download ends
# Encoder for each output extension. WebM cannot hold H.264, so it gets VP9: at CRF 18 it matches
# x264's hardest frames on real collage footage, in 40-75% of the size and about twice the encode time.
ENCODERS = {".mp4": H264 + FASTSTART, ".m4v": H264 + FASTSTART, ".mov": H264 + FASTSTART,
            ".mkv": H264, ".avi": H264, ".ts": H264,
            ".webm": ["-c:v", "libvpx-vp9", "-crf", "18", "-b:v", "0", "-row-mt", "1", "-cpu-used", "2"]}
BT2020_LUMA = (0.2627, 0.678, 0.0593)
BT2020_TO_BT709 = ((1.660491, -0.587641, -0.072850),  # linear-light primaries conversion, BT.2087
                   (-0.124550, 1.132900, -0.008349),
                   (-0.018151, -0.100579, 1.118730))


def discover(paths):
    """Return (video files, iCloud placeholders skipped, earlier collages skipped). A video with a
    .plan.json beside it is this script's own output, so it is not fed back in as a source.
    Unreadable directories are reported rather than silently skipped: on macOS that usually
    means the terminal/Python needs Files & Folders or Full Disk Access permission."""
    files, dataless, collages = [], 0, 0

    def unreadable(err):
        print(f"warning: cannot read {err.filename}: {err.strerror}", file=sys.stderr)

    for root in paths:
        root = Path(root).expanduser()
        if root.is_file():
            candidates = [root]
        elif root.is_dir():
            candidates = (Path(d, n) for d, _, names in os.walk(root, onerror=unreadable) for n in names)
        else:
            sys.exit(f"not found: {root}")
        for p in candidates:
            if p.suffix.lower() not in VIDEO_EXTS or p.name.startswith("."):
                continue
            try:
                flags = getattr(p.stat(), "st_flags", 0)
            except OSError as err:  # a broken symlink, or a file that vanished mid-walk
                print(f"warning: skipping {p}: {err.strerror}", file=sys.stderr)
                continue
            if flags & SF_DATALESS:
                dataless += 1
                continue
            if p.with_name(p.name + ".plan.json").exists():
                collages += 1
                continue
            files.append(p.resolve())
    return sorted(set(files)), dataless, collages


def probe(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None
    try:
        return video_meta(json.loads(out.stdout))
    except (ValueError, KeyError, TypeError):
        return None  # odd metadata: skip this file rather than abort the whole run


def video_meta(info):
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        return None
    duration = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
    if not 0 < duration < math.inf:
        return None
    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = round(float(sd["rotation"]))
    rotation = rotation or round(float(video.get("tags", {}).get("rotate", 0)))
    w, h = int(video["width"]), int(video["height"])
    # Untagged video decodes as BT.601 in ffmpeg, but players treat untagged HD as BT.709: match them.
    tagged = video.get("color_space") not in (None, "", "unknown", "reserved")
    matrix = "" if tagged else "bt709" if w >= 1280 or h > 576 else "bt601"
    num, _, den = (video.get("sample_aspect_ratio") or "1:1").partition(":")
    sar = (int(num) / int(den)) if num.isdigit() and den.isdigit() and int(num) and int(den) else 1.0
    w = round(w * sar)  # display width; clips apply the same SAR correction before cropping
    if w < 2 or h < 2:
        return None  # no window to cut (checked after SAR, which can squeeze the width under 2)
    if abs(rotation) % 180 == 90:
        w, h = h, w  # ffmpeg auto-rotates on decode, so plan in display orientation
    return {"duration": duration, "width": w, "height": h, "sar": sar, "matrix": matrix,
            "hdr": HDR_TRANSFERS.get(video.get("color_transfer"), ""),
            "interlaced": video.get("field_order") in ("tt", "bb", "tb", "bt")}


def cached_meta_ok(meta):
    """Is a cache entry something video_meta() could have returned: None or a well-formed dict?"""
    if meta is None:
        return True  # probed before and not a usable video
    try:
        return (0 < meta["duration"] < math.inf and meta["sar"] > 0 and meta["width"] >= 2
                and meta["height"] >= 2 and meta["matrix"] in ("", "bt709", "bt601")
                and meta["hdr"] in ("", *HDR_TRANSFERS.values()) and isinstance(meta["interlaced"], bool))
    except (KeyError, TypeError):
        return False


def cache_path():
    """The ffprobe cache file: under $XDG_CACHE_HOME if set, else the platform's per-user cache folder."""
    if os.environ.get("XDG_CACHE_HOME"):
        base = Path(os.environ["XDG_CACHE_HOME"])
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return base / "home-movie-collage" / "probe-cache.json"


def probe_all(files, jobs):
    cache_file = cache_path()
    try:
        cache = json.loads(cache_file.read_text())
    except (OSError, ValueError):
        cache = {}  # missing, unreadable or corrupt: probing again is cheap
    version = f"v{PROBE_VERSION}|"
    # Keep only well-formed entries from this version; anything else is probed again.
    cache = {k: v for k, v in cache.items()
             if k.startswith(version) and cached_meta_ok(v)} if isinstance(cache, dict) else {}
    keyed = {}
    for f in files:
        st = f.stat()
        keyed[f] = f"{version}{f}|{st.st_size}|{int(st.st_mtime)}"
    missing = [f for f in files if keyed[f] not in cache]
    if missing:
        print(f"probing {len(missing)} new file(s)…", file=sys.stderr)
        with ThreadPoolExecutor(jobs) as pool:
            for f, meta in zip(missing, pool.map(probe, missing)):
                cache[keyed[f]] = meta
        # Write a temp file and rename it, so an interrupted or concurrent run can't leave half a file.
        tmp = cache_file.with_name(f"{cache_file.name}.{os.getpid()}")
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(cache))
            os.replace(tmp, cache_file)
        except OSError as err:
            print(f"warning: cannot save the probe cache: {err.strerror}", file=sys.stderr)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
    return {f: cache[keyed[f]] for f in files if cache.get(keyed[f])}


def shortest_fade(args):
    """--fade-min in whole frames, rounded up, and never under 2: one blended frame is no fade."""
    return max(2, math.ceil(args.fade_min * args.fps - 1e-6))


def fit_fade(frames, clip_frames, prev_frames, shortest):
    """Fit a crossfade of `frames` between two clips, or return 0 for a hard cut. A fade may take
    at most 45% of either clip, so a clip's fade-in and fade-out never meet and xfade never runs
    past a clip's last frame (it would silently drop the clip). When that leaves no room for the
    shortest allowed fade, the clips are joined with a hard cut instead of a shorter fade."""
    room = int(0.45 * min(clip_frames, prev_frames))
    if not frames or room < shortest:
        return 0
    return max(shortest, min(int(frames), room))


def plan_clips(sources, args, rng):
    out_w, out_h = args.size
    aspect = out_w / out_h
    usable = [(p, m) for p, m in sources.items() if m["duration"] >= args.min_source]
    if not usable:
        sys.exit("no usable sources (all shorter than --min-source)")
    # Longer videos come up more often, by the square root of their length so short ones still
    # appear, and never the same video twice in a row (unless it is the only one).
    weights = [math.sqrt(m["duration"]) for _, m in usable]

    # Lengths and fades are whole frames at --fps, so the xfade offsets computed in assemble()
    # land exactly on frames instead of drifting off the end of a group.
    fps, clips, timeline, shortest = args.fps, [], 0, shortest_fade(args)
    while timeline < args.duration * fps:
        prev = clips[-1]["source"] if clips and len(usable) > 1 else None
        path, meta = rng.choices(usable, [0 if str(p) == prev else w for (p, _), w in zip(usable, weights)])[0]
        speed = 1.0
        if rng.random() < args.speed_prob:
            speed = math.exp(rng.uniform(math.log(args.speed_min), math.log(args.speed_max)))
        # `span` is the stretch of source consumed; the clip plays it back in span / speed seconds.
        span = min(rng.uniform(args.clip_min, args.clip_max) * speed, meta["duration"] - 0.05)
        # A lone frame has no duration and vanishes when joined. The 1e-6 absorbs float noise, or
        # (3 * speed) / speed * 30 can come out as 89.99999 and a fixed 3 s clip loses a frame.
        frames = max(2, int(span / speed * fps + 1e-6))
        length = frames / fps
        span = length * speed
        start = rng.uniform(0, max(0.0, meta["duration"] - span))

        angle = 0.0
        if rng.random() < args.rotate_prob:
            angle = rng.uniform(-args.rotate_max, args.rotate_max)
        cos_a, sin_a = abs(math.cos(math.radians(angle))), abs(math.sin(math.radians(angle)))

        # Fixed zoom window in source display pixels, at the output aspect ratio. A rotated
        # window needs its bounding box inside the frame (no black corners), so zoom further
        # in when it would not fit.
        zoom = math.exp(rng.uniform(math.log(args.zoom_min), math.log(args.zoom_max)))
        sw, sh = meta["width"], meta["height"]
        cw = min(sw, sh * aspect) / zoom
        ch = cw / aspect
        fit = min(1.0, sw / (cw * cos_a + ch * sin_a), sh / (cw * sin_a + ch * cos_a))
        cw, ch = max(2, math.floor(cw * fit)), max(2, math.floor(ch * fit))
        bw = min(sw, math.ceil(cw * cos_a + ch * sin_a))
        bh = min(sh, math.ceil(cw * sin_a + ch * cos_a))
        bx = rng.randint(0, sw - bw)
        by = rng.randint(0, sh - bh)

        fade = 0
        if clips and rng.random() < args.fade_prob:
            drawn = rng.uniform(args.fade_min, args.fade_max) * fps
            fade = fit_fade(drawn, frames, clips[-1]["frames"], shortest)
        clips.append({
            "source": str(path),
            "start": round(start, 3),
            "length": round(length, 4),
            "frames": frames,
            "speed": round(speed, 3),
            "zoom": round(min(sw, sh * aspect) / cw, 3),
            "rotation": round(angle, 2),
            "window": [cw, ch],
            "box": [bw, bh, bx, by],
            "fade_in": round(fade / fps, 4),
            "fade_frames": fade,
            "sar": meta["sar"],
            "source_size": [sw, sh],
            "matrix": meta["matrix"],
            "hdr": meta["hdr"],
            "interlaced": meta["interlaced"],
        })
        timeline += frames - fade
    return clips


def lut(expr):
    return "lutrgb=" + ":".join(f"{c}='{expr}'" for c in "rgb")


def hdr_to_sdr(hdr, w, h):
    """Filters that scale an HLG or PQ frame (BT.2020, 10-bit) to w x h as BT.709 SDR. This
    ffmpeg has no zscale or libplacebo, and its scale filter's own colour management mutes
    saturated colours, so it is done by hand in 16-bit linear RGB per BT.2100 and BT.2408:
    decode to display light with 1.0 at SDR white (HLG 75%, PQ 203 nits), convert the primaries,
    roll highlights off above 95% of white, and encode with gamma 2.4."""
    x = "(val/maxval)"
    if hdr == "hlg":
        # Inverse OETF, then the OOTF for a 1000-nit display (system gamma 1.2): each pixel is
        # scaled by its luminance^0.2, which takes a split and a multiply blend. Stored as nits/1000.
        a, b, c = 0.17883277, 0.28466892, 0.55991073
        top = 1000 / 203
        luma = "colorchannelmixer=" + ":".join(f"{o}{i}={k}" for o in "rgb" for i, k in zip("rgb", BT2020_LUMA))
        decode = (lut(f"maxval*if(lte({x},0.5),{x}*{x}/3,(exp(({x}-{c})/{a})+{b})/12)") +
                  f",split[e][f];[f]{luma},{lut(f'maxval*pow({x},0.2)')}[y];[e][y]blend=all_mode=multiply")
    else:
        # The PQ EOTF in units of 203 nits, clipped at twice that and stored halved.
        top = 2
        p = f"pow({x},1/78.84375)"
        light = f"{10000 / 203}*pow(max({p}-0.8359375,0)/(18.8515625-18.6875*{p}),1/0.1593017578125)"
        decode = lut(f"maxval*min({light},{top})/{top}")
    mix = "colorchannelmixer=" + ":".join(
        f"{o}{i}={k}" for o, row in zip("rgb", BT2020_TO_BT709) for i, k in zip("rgb", row))
    v = f"({x}*{top})"
    knee = f"min(if(lte({v},0.95),{v},0.95+0.05*(1-exp(-({v}-0.95)/0.05))),1)"
    return (f"scale={w}:{h}:flags=bicubic+accurate_rnd+full_chroma_int:in_color_matrix=bt2020:in_range=tv:"
            f"out_range=pc,format=gbrp16le,{decode},{mix},{lut(f'maxval*pow({knee},1/2.4)')},"
            "scale=out_color_matrix=bt709:out_range=tv:flags=accurate_rnd")


def clip_filter(clip, args, trim=0.0):
    """Video filtergraph: deinterlace camcorder footage, drop `trim` seconds decoded before the
    clip's start, retime, crop the window's bounding box, rotate so the window is upright and cut
    out, then scale it up to the output size as BT.709 SDR. Timestamps are always rebased: a seek
    can deliver a first frame after 0."""
    out_w, out_h = args.size
    cw, ch = clip["window"]
    bw, bh, bx, by = clip["box"]
    # Deinterlace before anything resamples the picture, while the two fields are still intact.
    chain = ["bwdif=mode=send_frame:deint=all"] if clip.get("interlaced") else []
    if trim:
        chain.append(f"trim=start={trim:.3f}")
    chain.append(f"setpts=(PTS-STARTPTS)/{clip['speed']}")
    if abs(clip["sar"] - 1.0) > 1e-3:
        chain.append(f"scale={clip['source_size'][0]}:ih,setsar=1")
    chain.append(f"crop={bw}:{bh}:{bx}:{by}")
    if clip["rotation"]:
        chain.append(f"rotate={math.radians(clip['rotation']):.5f}:ow={cw}:oh={ch}")
    # Everything leaves as BT.709 SDR: HDR is tone-mapped, and SD footage's BT.601 matrix converted.
    matrix = f"in_color_matrix={clip['matrix']}:" if clip.get("matrix") else ""
    chain += [
        hdr_to_sdr(clip["hdr"], out_w, out_h) if clip.get("hdr")
        else f"scale={out_w}:{out_h}:flags=bicubic:{matrix}out_color_matrix=bt709:out_range=tv",
        "setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv",
        "setsar=1",
        f"fps={args.fps}",
        f"tpad=stop_mode=clone:stop_duration={clip['length']}",
        "format=yuv420p",
    ]
    return ",".join(chain)


def count_frames(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    # MPEG-TS lists its stream twice (inside its program and on its own): take the first count.
    counts = out.stdout.split()
    return int(counts[0]) if counts and counts[0].isdigit() else 0


def render_clip(index, clip, args, workdir):
    """A fast seek into an MPEG-TS (which has no index) lands after the keyframe it needed, so
    decoding starts at the next one: a clip from camcorder footage begins a few frames late, and
    one near the end of a file can come out empty while ffmpeg still exits 0. Every attempt is
    checked by counting frames; retries seek further back, then from the top, and trim up to
    the planned start."""
    # NUT keeps exact 1/fps timestamps. Matroska rounds them to whole milliseconds, so a long run
    # of hard cuts gains or loses about a frame per hundred clips when assemble() joins them.
    out = workdir / f"clip{index:05d}.nut"
    start = clip["start"]
    # Jobs share the cores: left alone, each ffmpeg decoding 4K HEVC runs ~70 threads, and five of
    # them starve the whole computer. On macOS the hardware decoder halves the work (same frames).
    threads = str(max(1, (os.cpu_count() or 2) // args.jobs))
    hwaccel = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []
    for seek in sorted({start, max(0.0, start - 10), 0.0}, reverse=True):
        # Damaged video can stop the hardware decoder (or garble its frames) where software decoding
        # conceals the damage, so a hardware attempt that reports a failure is redone in software.
        for hw in ([hwaccel, []] if hwaccel else [[]]):
            cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-filter_threads", threads, *hw,
                   "-threads", threads, "-ss", f"{seek:.3f}", "-i", clip["source"],
                   "-vf", clip_filter(clip, args, trim=start - seek), "-an", "-frames:v", str(clip["frames"]),
                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-threads", threads, str(out)]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0 and not hw:
                raise RuntimeError((res.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
            if (res.returncode == 0 and "hardware accelerator failed" not in res.stderr
                    and count_frames(out) == clip["frames"]):
                return out
    raise RuntimeError(f"no frames decoded at {start}s")


def assemble(clip_files, clips, args):
    """Hard cuts are concatenated into groups; groups are joined with xfade."""
    groups = []
    for i, clip in enumerate(clips):
        if i == 0 or clip["fade_frames"] > 0:
            groups.append({"members": [], "fade_in": clip["fade_frames"], "length": 0})
        groups[-1]["members"].append(i)
        groups[-1]["length"] += clip["frames"]

    parts = []
    for g, group in enumerate(groups):
        ins = "".join(f"[{i}:v]" for i in group["members"])
        parts.append(f"{ins}concat=n={len(group['members'])}:v=1:a=0,settb=AVTB,fps={args.fps}[g{g}]")

    fps, cur, cur_len = args.fps, "[g0]", groups[0]["length"]
    for g in range(1, len(groups)):
        d = groups[g]["fade_in"]
        parts.append(f"{cur}[g{g}]xfade=transition=fade:duration={d / fps:.6f}:"
                     f"offset={(cur_len - d) / fps:.6f}[x{g}]")
        cur = f"[x{g}]"
        cur_len += groups[g]["length"] - d

    script = args.workdir / "assemble.filter"
    script.write_text(";\n".join(parts))
    # Every clip is an open input: lift the open-file limit (macOS Terminal defaults to 256), and
    # decode each on one thread, or ffmpeg hits macOS's 4096 threads per process. That still leaves
    # about 6 threads per clip; single-threaded filters bring it to 2 (1010 clips used to fail, 2000
    # now work) but assemble about a third slower, so only long collages get them.
    if resource:
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        need = min(len(clip_files) + 256, hard)  # RLIM_INFINITY is just a very large number
        if soft < need:
            try:
                resource.setrlimit(resource.RLIMIT_NOFILE, (need, hard))
            except (ValueError, OSError):
                pass  # ffmpeg will say so if it really runs out
    # Encode beside the output and rename on success, so a failed run never clobbers a good file.
    # (abspath, not resolve(): before Python 3.10, Windows leaves a missing file's path relative.)
    part = Path(os.path.abspath(args.output)).with_name(f".{args.output.stem}.part{args.output.suffix}")
    # ffmpeg runs in the work folder so inputs are bare names: Windows caps a command line at
    # 32,767 characters, which full temp paths would reach at about 400 clips.
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-stats", "-y"]
    for f in clip_files:
        cmd += ["-threads", "1", "-i", f.name]
    if len(clip_files) > 400:
        cmd += ["-filter_complex_threads", "1"]
    cmd += ["-/filter_complex", script.name, "-map", cur, "-an"]
    cmd += ENCODERS[args.output.suffix.lower()] + ["-pix_fmt", "yuv420p", "-colorspace", "bt709",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv", str(part)]
    try:
        subprocess.run(cmd, check=True, cwd=args.workdir)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    try:
        os.replace(part, args.output)
    except OSError as err:  # on Windows, e.g. while the previous collage is still open in a player
        sys.exit(f"cannot replace {args.output} ({err.strerror}); the new collage was kept as {part}")
    return cur_len


def parse_size(text):
    if text.strip().lower() in SIZE_PRESETS:
        return SIZE_PRESETS[text.strip().lower()]
    w, _, h = text.strip().lower().partition("x")
    w, h = w.strip(), h.strip()
    if not (w.isdigit() and h.isdigit() and int(w) and int(h)):
        raise argparse.ArgumentTypeError(
            f"{text!r} is not WxH (e.g. 1280x720) or one of {', '.join(SIZE_PRESETS)}")
    w, h = int(w), int(h)
    if w % 2 or h % 2:
        raise argparse.ArgumentTypeError("size must have even dimensions")
    return w, h


def check_ffmpeg(output_ext):
    """Stop early, with an install hint, if ffmpeg is missing, too old or lacks an encoder we need."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if not shutil.which(tool)]
    if missing:
        sys.exit(f"{' and '.join(missing)} not found: {FFMPEG_HINT}")
    try:
        banner = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True).stdout
        encoders = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                  capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError) as err:
        sys.exit(f"ffmpeg is there but does not run ({err}): {FFMPEG_HINT}")
    version = re.match(r"ffmpeg version (n?(\d+)\.\S*)", banner)  # git builds print no number: let them by
    if version and int(version.group(2)) < MIN_FFMPEG:
        sys.exit(f"this needs ffmpeg {MIN_FFMPEG} or newer, but found {version.group(1)}: {FFMPEG_HINT}")
    for name, use in (("libx264", "every clip"), (ENCODERS[output_ext][1], f"{output_ext} output")):
        if name not in encoders:
            sys.exit(f"this ffmpeg has no {name} encoder, which {use} needs: {FFMPEG_HINT}")


def lower_priority():
    """Render below everything else, so the computer stays usable; ffmpeg still gets idle cores."""
    if hasattr(os, "nice"):
        os.nice(10)
    elif sys.platform == "win32":  # child processes inherit a below-normal priority class
        import ctypes
        ctypes.windll.kernel32.SetPriorityClass(ctypes.windll.kernel32.GetCurrentProcess(), 0x4000)


def package_version():
    try:
        return metadata.version("home-movie-collage")
    except metadata.PackageNotFoundError:
        return "(run from source)"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="video files and/or folders (searched recursively)")
    ap.add_argument("-o", "--output", type=Path, default=Path("collage.mp4"),
                    help=f"output video ({', '.join(ENCODERS)}); only ever replaces an earlier collage")
    ap.add_argument("-d", "--duration", type=float, default=60, help="target length in seconds")
    ap.add_argument("--seed", type=int, help="random seed (printed if omitted)")
    ap.add_argument("--size", type=parse_size, default=SIZE_PRESETS["landscape"],
                    help="output WxH, or " + ", ".join(f"{k} ({w}x{h})" for k, (w, h) in SIZE_PRESETS.items())
                    + "; default landscape")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--clip-min", type=float, help=f"shortest clip, seconds (default {RANGES['clip'][0]:g})")
    ap.add_argument("--clip-max", type=float, help=f"longest clip, seconds (default {RANGES['clip'][1]:g})")
    ap.add_argument("--clip", type=float, help="give every clip this length instead of a random one")
    ap.add_argument("--min-source", type=float, default=1.0, help="ignore sources shorter than this")
    ap.add_argument("--zoom-min", type=float, help=f"smallest zoom factor (default {RANGES['zoom'][0]:g})")
    ap.add_argument("--zoom-max", type=float, help=f"largest zoom factor (default {RANGES['zoom'][1]:g})")
    ap.add_argument("--zoom", type=float, help="zoom every clip this much instead of a random amount")
    ap.add_argument("--speed-prob", type=float, default=0.4, help="chance a clip is retimed")
    ap.add_argument("--speed-min", type=float, default=0.33, help="slowest playback rate")
    ap.add_argument("--speed-max", type=float, default=3.0, help="fastest playback rate")
    ap.add_argument("--rotate-prob", type=float, default=0.4, help="chance a clip is rotated")
    ap.add_argument("--rotate-max", type=float, default=180.0,
                    help="rotated clips get a random angle in ±this many degrees")
    ap.add_argument("--fade-prob", type=float, default=0.35, help="chance a clip crossfades in")
    ap.add_argument("--fade-min", type=float, help=f"shortest crossfade, seconds (default {RANGES['fade'][0]:g}); "
                    "clips too short for one get a hard cut")
    ap.add_argument("--fade-max", type=float, help=f"longest crossfade, seconds (default {RANGES['fade'][1]:g})")
    ap.add_argument("--fade", type=float,
                    help="make every crossfade this long instead of a random length (--fade-prob sets how often)")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--keep-temp", action="store_true", help="keep rendered clips")
    ap.add_argument("--version", action="version", version=f"%(prog)s {package_version()}")
    args = ap.parse_args()
    # --clip, --zoom and --fade pin a range to one value; otherwise unset bounds take their defaults.
    for name, (low, high) in RANGES.items():
        fixed, lo, hi = getattr(args, name), getattr(args, f"{name}_min"), getattr(args, f"{name}_max")
        if fixed is not None:
            if lo is not None or hi is not None:
                ap.error(f"use --{name} or --{name}-min/--{name}-max, not both")
            lo = hi = fixed
        setattr(args, f"{name}_min", low if lo is None else lo)
        setattr(args, f"{name}_max", high if hi is None else hi)
    if not all(math.isfinite(v) for v in vars(args).values() if isinstance(v, float)):
        ap.error("numbers must be finite (inf and nan are not allowed)")
    if not 0 < args.clip_min <= args.clip_max:
        ap.error(f"need 0 < clip-min <= clip-max (they are {args.clip_min:g} and {args.clip_max:g})")
    if not 1 <= args.zoom_min <= args.zoom_max:
        ap.error(f"need 1 <= zoom-min <= zoom-max (they are {args.zoom_min:g} and {args.zoom_max:g})")
    if not (0 < args.speed_min <= args.speed_max):
        ap.error("need 0 < speed-min <= speed-max")
    if not (0 <= args.fade_min <= args.fade_max):
        ap.error(f"need 0 <= fade-min <= fade-max (they are {args.fade_min:g} and {args.fade_max:g})")
    if not all(0 <= p <= 1 for p in (args.speed_prob, args.rotate_prob, args.fade_prob)):
        ap.error("--speed-prob, --rotate-prob and --fade-prob must be between 0 and 1")
    if not (args.duration > 0 and args.fps > 0 and args.jobs >= 1 and args.rotate_max >= 0):
        ap.error("need --duration > 0, --fps > 0, --jobs >= 1 and --rotate-max >= 0")
    if args.output.suffix.lower() not in ENCODERS:
        ap.error(f"-o {args.output.name}: the name must end in one of {', '.join(ENCODERS)}")
    if not args.output.parent.is_dir():
        ap.error(f"-o {args.output}: the folder {args.output.parent} does not exist")
    check_ffmpeg(args.output.suffix.lower())

    seed = args.seed if args.seed is not None else random.randrange(2**31)
    rng = random.Random(seed)

    # Only ever overwrite an earlier collage: anything else at -o may be an irreplaceable original.
    plan_path = args.output.with_name(args.output.name + ".plan.json")
    if args.output.exists() and not plan_path.exists():
        sys.exit(f"{args.output} exists and is not an earlier collage (no {plan_path.name}); pick another -o")

    files, dataless, collages = discover(args.sources)
    if dataless:
        print(f"skipping {dataless} iCloud placeholder(s) not downloaded to this Mac", file=sys.stderr)
    if collages:
        print(f"skipping {collages} earlier collage(s) (they have a .plan.json beside them)", file=sys.stderr)
    sources = probe_all(files, args.jobs)
    print(f"{len(sources)} usable source video(s); seed {seed}", file=sys.stderr)
    if not sources:
        sys.exit("nothing to work with")

    clips = plan_clips(sources, args, rng)
    lower_priority()
    args.workdir = Path(tempfile.mkdtemp(prefix="collage-"))
    try:
        print(f"rendering {len(clips)} clips with {args.jobs} jobs…", file=sys.stderr)
        done, lock, tty = iter(range(1, len(clips) + 1)), threading.Lock(), sys.stderr.isatty()

        def progress(_):  # one line redrawn in a terminal; in a log, a line every tenth of the way
            with lock:
                n = next(done)
                if tty:
                    print(f"\r{n}/{len(clips)} clips done", end="", file=sys.stderr, flush=True)
                elif n == len(clips) or n % max(1, len(clips) // 10) == 0:
                    print(f"{n}/{len(clips)} clips done", file=sys.stderr, flush=True)

        with ThreadPoolExecutor(args.jobs) as pool:
            futures = [pool.submit(render_clip, i, clip, args, args.workdir) for i, clip in enumerate(clips)]
            for future in futures:
                future.add_done_callback(progress)
        if tty:
            print(file=sys.stderr)  # end the progress line
        kept, clip_files = [], []
        for i, (clip, future) in enumerate(zip(clips, futures)):
            try:
                clip_files.append(future.result())
            except RuntimeError as err:
                print(f"warning: dropping clip {i} ({clip['source']} at {clip['start']}s): {err}",
                      file=sys.stderr)
                clip.update(dropped=str(err), fade_frames=0, fade_in=0.0)
                continue
            # A dropped clip leaves its neighbours adjacent, so re-fit the fade to the new pair.
            prev = kept[-1] if kept else None
            clip["fade_frames"] = (fit_fade(clip["fade_frames"], clip["frames"], prev["frames"], shortest_fade(args))
                                   if prev else 0)
            clip["fade_in"] = round(clip["fade_frames"] / args.fps, 4)
            kept.append(clip)
        if not kept:
            sys.exit("every clip failed to render")
        print("assembling…", file=sys.stderr)
        try:
            planned = assemble(clip_files, kept, args)
        except subprocess.CalledProcessError:
            sys.exit(f"ffmpeg failed joining the {len(kept)} clips (its error is above); "
                     f"if that is a resource limit, try a shorter -d or a longer --clip-min")
        # Written once the video is in place, so the plan beside -o always describes that video.
        plan_path.write_text(json.dumps({"seed": seed, "argv": sys.argv[1:], "clips": clips}, indent=1))
        actual = count_frames(args.output)
        print(f"wrote {args.output} ({actual / args.fps:.1f}s), plan in {plan_path}", file=sys.stderr)
        if actual != planned:
            print(f"warning: the plan was {planned} frames ({planned / args.fps:.1f}s); "
                  f"the output has {actual}", file=sys.stderr)
        if len(kept) < len(clips):
            print(f"warning: {len(clips) - len(kept)} of {len(clips)} clips were dropped, so the output is "
                  f"shorter than planned", file=sys.stderr)
    finally:
        if args.keep_temp:
            print(f"clips kept in {args.workdir}", file=sys.stderr)
        else:
            shutil.rmtree(args.workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
