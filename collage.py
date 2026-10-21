#!/usr/bin/env python3
"""Build a random, heavily-zoomed, silent video collage from a folder (or folders) of videos.

Pipeline:
  1. discover video files under the given paths (skipping iCloud placeholders and earlier collages),
  2. ffprobe each one (cached in .probe-cache.json next to this script),
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
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mkv", ".avi", ".webm", ".mts", ".m2ts", ".3gp"}
SF_DATALESS = 0x40000000  # macOS st_flags bit for iCloud files whose data is not on disk
CACHE_PATH = Path(__file__).with_name(".probe-cache.json")


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
            if getattr(p.stat(), "st_flags", 0) & SF_DATALESS:
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
    info = json.loads(out.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    if video is None:
        return None
    duration = float(video.get("duration") or info.get("format", {}).get("duration") or 0)
    if duration <= 0:
        return None
    rotation = 0
    for sd in video.get("side_data_list", []):
        if "rotation" in sd:
            rotation = int(sd["rotation"])
    rotation = rotation or int(video.get("tags", {}).get("rotate", 0))
    w, h = int(video["width"]), int(video["height"])
    num, _, den = (video.get("sample_aspect_ratio") or "1:1").partition(":")
    sar = (int(num) / int(den)) if num.isdigit() and den.isdigit() and int(num) and int(den) else 1.0
    w = round(w * sar)  # display width; clips apply the same SAR correction before cropping
    if abs(rotation) % 180 == 90:
        w, h = h, w  # ffmpeg auto-rotates on decode, so plan in display orientation
    return {"duration": duration, "width": w, "height": h, "sar": sar}


def probe_all(files, jobs):
    cache = json.loads(CACHE_PATH.read_text()) if CACHE_PATH.exists() else {}
    keyed = {}
    for f in files:
        st = f.stat()
        keyed[f] = f"{f}|{st.st_size}|{int(st.st_mtime)}"
    missing = [f for f in files if keyed[f] not in cache]
    if missing:
        print(f"probing {len(missing)} new file(s)…", file=sys.stderr)
        with ThreadPoolExecutor(jobs) as pool:
            for f, meta in zip(missing, pool.map(probe, missing)):
                cache[keyed[f]] = meta
        CACHE_PATH.write_text(json.dumps(cache))
    return {f: cache[keyed[f]] for f in files if cache.get(keyed[f])}


def fit_fade(frames, clip_frames, prev_frames):
    """Cap a crossfade at 45% of both clips it overlaps. xfade silently drops the outgoing side
    when its offset passes that side's last frame, so a fade under 2 frames becomes a hard cut."""
    frames = int(min(frames, 0.45 * min(clip_frames, prev_frames)))
    return frames if frames >= 2 else 0


def plan_clips(sources, args, rng):
    out_w, out_h = args.size
    aspect = out_w / out_h
    usable = [(p, m) for p, m in sources.items() if m["duration"] >= args.min_source]
    if not usable:
        sys.exit("no usable sources (all shorter than --min-source)")

    # Lengths and fades are whole frames at --fps, so the xfade offsets computed in assemble()
    # land exactly on frames instead of drifting off the end of a group.
    fps, clips, timeline = args.fps, [], 0
    while timeline < args.duration * fps:
        path, meta = rng.choice(usable)
        speed = 1.0
        if rng.random() < args.speed_prob:
            speed = math.exp(rng.uniform(math.log(args.speed_min), math.log(args.speed_max)))
        # `span` is the stretch of source consumed; the clip plays it back in span / speed seconds.
        span = min(rng.uniform(args.clip_min, args.clip_max) * speed, meta["duration"] - 0.05)
        frames = max(1, int(span / speed * fps))
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
            fade = fit_fade(rng.uniform(args.fade_min, args.fade_max) * fps, frames, clips[-1]["frames"])
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
        })
        timeline += frames - fade
    return clips


def clip_filter(clip, args, trim=0.0):
    """Video filter chain: drop `trim` seconds decoded before the clip's start, retime, crop the
    window's bounding box, rotate so the window is upright and cut out, then scale it up to the
    output size. Timestamps are always rebased: a seek can deliver a first frame after 0."""
    out_w, out_h = args.size
    cw, ch = clip["window"]
    bw, bh, bx, by = clip["box"]
    chain = [f"trim=start={trim:.3f}"] if trim else []
    chain.append(f"setpts=(PTS-STARTPTS)/{clip['speed']}")
    if abs(clip["sar"] - 1.0) > 1e-3:
        chain.append(f"scale={clip['source_size'][0]}:ih,setsar=1")
    chain.append(f"crop={bw}:{bh}:{bx}:{by}")
    if clip["rotation"]:
        chain.append(f"rotate={math.radians(clip['rotation']):.5f}:ow={cw}:oh={ch}")
    chain += [
        f"scale={out_w}:{out_h}:flags=bicubic",
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
    return int(out.stdout) if out.stdout.strip().isdigit() else 0


def render_clip(index, clip, args, workdir):
    """A fast seek into an MPEG-TS (which has no index) lands after the keyframe it needed, so
    decoding starts at the next one: a clip from camcorder footage begins a few frames late, and
    one near the end of a file can come out empty while ffmpeg still exits 0. Every attempt is
    checked by counting frames; retries seek further back, then from the top, and trim up to
    the planned start."""
    out = workdir / f"clip{index:05d}.mkv"
    start = clip["start"]
    for seek in sorted({start, max(0.0, start - 10), 0.0}, reverse=True):
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{seek:.3f}", "-i", clip["source"],
               "-vf", clip_filter(clip, args, trim=start - seek), "-an", "-frames:v", str(clip["frames"]),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", str(out)]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError((res.stderr.strip().splitlines() or ["ffmpeg failed"])[-1])
        if count_frames(out) == clip["frames"]:
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
    # Encode beside the output and rename on success, so a failed run never clobbers a good file.
    part = args.output.with_name(f".{args.output.stem}.part{args.output.suffix}")
    cmd = ["ffmpeg", "-nostdin", "-v", "error", "-stats", "-y"]
    for f in clip_files:
        cmd += ["-i", str(f)]
    cmd += ["-/filter_complex", str(script), "-map", cur, "-an"]
    cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-movflags", "+faststart", str(part)]
    try:
        subprocess.run(cmd, check=True)
        os.replace(part, args.output)
    finally:
        part.unlink(missing_ok=True)
    return cur_len / fps


def parse_size(text):
    w, _, h = text.lower().partition("x")
    w, h = int(w), int(h)
    if w % 2 or h % 2:
        raise argparse.ArgumentTypeError("size must have even dimensions")
    return w, h


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="+", help="video files and/or folders (searched recursively)")
    ap.add_argument("-o", "--output", type=Path, default=Path("collage.mp4"))
    ap.add_argument("-d", "--duration", type=float, default=60, help="target length in seconds")
    ap.add_argument("--seed", type=int, help="random seed (printed if omitted)")
    ap.add_argument("--size", type=parse_size, default=(1280, 720), help="output WxH")
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--clip-min", type=float, default=1.5, help="shortest clip, seconds")
    ap.add_argument("--clip-max", type=float, default=6.0, help="longest clip, seconds")
    ap.add_argument("--min-source", type=float, default=1.0, help="ignore sources shorter than this")
    ap.add_argument("--zoom-min", type=float, default=2.5, help="smallest zoom factor")
    ap.add_argument("--zoom-max", type=float, default=7.0, help="largest zoom factor")
    ap.add_argument("--speed-prob", type=float, default=0.4, help="chance a clip is retimed")
    ap.add_argument("--speed-min", type=float, default=0.33, help="slowest playback rate")
    ap.add_argument("--speed-max", type=float, default=3.0, help="fastest playback rate")
    ap.add_argument("--rotate-prob", type=float, default=0.4, help="chance a clip is rotated")
    ap.add_argument("--rotate-max", type=float, default=180.0,
                    help="rotated clips get a random angle in ±this many degrees")
    ap.add_argument("--fade-prob", type=float, default=0.35, help="chance a clip crossfades in")
    ap.add_argument("--fade-min", type=float, default=0.4, help="shortest crossfade, seconds")
    ap.add_argument("--fade-max", type=float, default=2.5, help="longest crossfade, seconds")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2))
    ap.add_argument("--keep-temp", action="store_true", help="keep rendered clips")
    args = ap.parse_args()
    if not (0 < args.clip_min <= args.clip_max and 1 <= args.zoom_min <= args.zoom_max):
        ap.error("need 0 < clip-min <= clip-max and 1 <= zoom-min <= zoom-max")
    if not (0 < args.speed_min <= args.speed_max):
        ap.error("need 0 < speed-min <= speed-max")

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
    args.workdir = Path(tempfile.mkdtemp(prefix="collage-"))
    try:
        print(f"rendering {len(clips)} clips with {args.jobs} jobs…", file=sys.stderr)
        with ThreadPoolExecutor(args.jobs) as pool:
            futures = [pool.submit(render_clip, i, clip, args, args.workdir) for i, clip in enumerate(clips)]
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
            clip["fade_frames"] = fit_fade(clip["fade_frames"], clip["frames"], prev["frames"]) if prev else 0
            clip["fade_in"] = round(clip["fade_frames"] / args.fps, 4)
            kept.append(clip)
        if not kept:
            sys.exit("every clip failed to render")
        print("assembling…", file=sys.stderr)
        planned = assemble(clip_files, kept, args)
        # Written once the video is in place, so the plan beside -o always describes that video.
        plan_path.write_text(json.dumps({"seed": seed, "argv": sys.argv[1:], "clips": clips}, indent=1))
        actual = count_frames(args.output) / args.fps
        print(f"wrote {args.output} ({actual:.1f}s), plan in {plan_path}", file=sys.stderr)
        if abs(actual - planned) > 1.5 / args.fps:
            print(f"warning: the plan was {planned:.1f}s long; the output is {actual:.1f}s", file=sys.stderr)
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
