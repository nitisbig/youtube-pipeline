#!/usr/bin/env python3
"""
editor.py - turn numbered still images + beat.json into an animated MP4 with FFmpeg.

What it does
    * Reads beat.json (image_id / start_time / end_time per scene) and builds a
      continuous timeline. Gaps between beats are filled by holding the previous
      image and overlaps are cut at the next beat's start, so the picture never
      drifts away from the narration.
    * Fits every image to the target frame (16:9, 9:16, 1:1, 4:5 or --size WxH)
      either by cropping (--fit cover) or letterboxing (--fit contain).
    * Applies one animation preset per scene:
        static   : none, fadein, fade
        motion   : zoom_in, zoom_out, ken_burns, drift, pan_left, pan_right, pan_up, pan_down
        slide-in : slide_left, slide_right, slide_up, slide_down
        random   : a different preset for every scene (--seed makes it repeatable)
      A beat.json entry may carry its own "animation" key to override the global choice.
    * --smoothness picks the easing curve, --zoom the motion strength and
      --transition adds a fade-in / fade-in+out on top of any motion.

Examples
    python3 editor.py --imagesource ~/Downloads/bulk --beatpath out/demo/beat.json --out out/demo/demo.mp4
    python3 editor.py ... --animation random --transition fade --aspect 9:16
    python3 editor.py ... --animation ken_burns --smoothness ease_out --zoom 0.25 --seed 42
    python3 editor.py --list-animations
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

SUPPORTED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

ASPECT_PRESETS: Dict[str, Tuple[int, int]] = {
    "16:9": (1920, 1080),
    "9:16": (1080, 1920),
    "1:1": (1080, 1080),
    "4:5": (1080, 1350),
    "4:3": (1440, 1080),
}

STATIC_ANIMATIONS = ("none", "fadein", "fade")
MOTION_ANIMATIONS = (
    "zoom_in", "zoom_out", "ken_burns", "drift",
    "pan_left", "pan_right", "pan_up", "pan_down",
)
SLIDE_ANIMATIONS = ("slide_left", "slide_right", "slide_up", "slide_down")
ANIMATIONS = STATIC_ANIMATIONS + MOTION_ANIMATIONS + SLIDE_ANIMATIONS + ("random",)
DEFAULT_RANDOM_POOL = MOTION_ANIMATIONS + SLIDE_ANIMATIONS

EASINGS = ("linear", "ease_in", "ease_out", "ease_in_out")
TRANSITIONS = ("none", "fadein", "fade")
FIT_MODES = ("cover", "contain")
ON_MISSING = ("hold", "skip", "error")

# Motion is rendered on an upscaled canvas: zoompan positions are integers,
# so working at 2x and scaling down hides the sub-pixel jitter.
UPSCALE = 2
KEN_BURNS_ANCHORS = [(0, 0), (1, 0), (0, 1), (1, 1), (0.5, 0), (0.5, 1), (0, 0.5), (1, 0.5)]

ANIMATION_HELP = {
    "none": "static image",
    "fadein": "static image, fades in",
    "fade": "static image, fades in and out",
    "zoom_in": "slow push in towards the centre",
    "zoom_out": "starts close, pulls back to the full frame",
    "ken_burns": "push in towards a random edge or corner",
    "drift": "gentle 2.5D float: light zoom with a soft sideways/vertical drift",
    "pan_left": "camera pans towards the left edge",
    "pan_right": "camera pans towards the right edge",
    "pan_up": "camera pans towards the top",
    "pan_down": "camera pans towards the bottom",
    "slide_left": "image slides in from the left",
    "slide_right": "image slides in from the right",
    "slide_up": "image slides up into place from the bottom",
    "slide_down": "image slides down into place from the top",
    "random": "a different preset for every scene (see --random-pool / --seed)",
}


class EditorError(Exception):
    """Anything that should end the run with a clean one-line message."""


@dataclass
class Segment:
    image_id: int
    image: Path
    start: float
    end: float
    animation: Optional[str] = None
    frames: int = 0

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class RenderOptions:
    width: int
    height: int
    fps: int
    animation: str
    transition: str
    easing: str
    fit: str
    zoom: float
    fade_duration: float
    slide_duration: float
    bg_color: str
    random_pool: Sequence[str]
    rng: random.Random
    preset: str
    crf: int


# --------------------------------------------------------------------------- #
# Inputs
# --------------------------------------------------------------------------- #


def natural_sort_key(path: Path):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", path.name)]


def check_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise EditorError("ffmpeg is not installed. Install it with: sudo apt install ffmpeg")


def image_id_from_name(path: Path) -> Optional[int]:
    """01.png -> 1, shot_007.webp -> 7, 'image (3).jpg' -> 3, None if no digits."""
    match = re.match(r"^(\d+)", path.stem)
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else None


def find_images(source: Path) -> Dict[int, Path]:
    if not source.exists():
        raise EditorError(f"image directory does not exist: {source}")
    if not source.is_dir():
        raise EditorError(f"not a directory: {source}")

    files = sorted(
        (f for f in source.iterdir() if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=natural_sort_key,
    )
    if not files:
        raise EditorError(f"no images found in: {source}")

    images: Dict[int, Path] = {}
    for file in files:
        image_id = image_id_from_name(file)
        if image_id is None:
            print(f"Warning: cannot determine an image id from '{file.name}', skipping.")
            continue
        if image_id in images:
            print(f"Warning: duplicate image id {image_id}: keeping '{images[image_id].name}', ignoring '{file.name}'.")
            continue
        images[image_id] = file

    if not images:
        raise EditorError(f"no images with a numeric id (01.png, 02.png, ...) found in: {source}")
    return images


def load_beat_file(beat_path: Path) -> List[dict]:
    """
    beat.json format:
        [{"image_id": 1, "start_time": 0, "end_time": 3.5, "animation": "zoom_in"}, ...]
    "animation" is optional and overrides --animation for that scene.
    """
    if not beat_path.exists():
        raise EditorError(f"beat file does not exist: {beat_path}")
    try:
        data = json.loads(beat_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditorError(f"invalid beat file {beat_path}: {exc}") from exc
    if not isinstance(data, list) or not data:
        raise EditorError("beat.json must contain a non-empty JSON array.")

    beats: List[dict] = []
    seen = set()
    for item in data:
        if not isinstance(item, dict):
            raise EditorError("every beat entry must be an object.")
        missing = {"image_id", "start_time", "end_time"} - item.keys()
        if missing:
            raise EditorError(f"beat entry is missing fields: {', '.join(sorted(missing))}")
        try:
            image_id = int(item["image_id"])
            start = float(item["start_time"])
            end = float(item["end_time"])
        except (TypeError, ValueError):
            raise EditorError(f"invalid beat entry: {item}")
        if start < 0 or end <= start:
            raise EditorError(f"image_id {image_id} has invalid timing: {start} -> {end}")
        if image_id in seen:
            raise EditorError(f"image_id {image_id} appears twice in beat.json")
        seen.add(image_id)

        animation = item.get("animation")
        if animation is not None:
            animation = str(animation).strip().lower()
            if animation not in ANIMATIONS:
                raise EditorError(f"image_id {image_id}: unknown animation '{animation}'")

        beats.append({"image_id": image_id, "start_time": start, "end_time": end, "animation": animation})
    return beats


# --------------------------------------------------------------------------- #
# Timeline
# --------------------------------------------------------------------------- #


def build_timeline(beats: List[dict], images: Dict[int, Path], on_missing: str) -> List[Segment]:
    """Turn beat entries into back-to-back segments that cover 0 .. last end.

    * The first scene starts at 0 even if the first beat starts later.
    * Each scene ends where the next one starts: gaps are filled by holding
      the image, overlaps are cut. Total length therefore equals the audio.
    * A beat without an image is either held by its neighbour (hold), dropped
      with its time (skip) or fatal (error).
    """
    ordered = sorted(beats, key=lambda b: (b["start_time"], b["image_id"]))
    segments: List[Segment] = []
    carry_start: Optional[float] = None

    for i, beat in enumerate(ordered):
        start = 0.0 if i == 0 else beat["start_time"]
        end = ordered[i + 1]["start_time"] if i + 1 < len(ordered) else beat["end_time"]
        if carry_start is not None:
            start = carry_start
            carry_start = None

        if end - start <= 1e-6:
            print(f"Warning: image_id {beat['image_id']} has no screen time after resolving overlaps, skipping.")
            continue

        image = images.get(beat["image_id"])
        if image is None:
            if on_missing == "error":
                raise EditorError(f"no image for image_id {beat['image_id']} - download it or use --on-missing hold")
            if on_missing == "skip":
                print(f"Warning: no image for image_id {beat['image_id']}; dropping {end - start:.2f}s (video will be shorter).")
                continue
            if segments:
                print(f"Warning: no image for image_id {beat['image_id']}; holding '{segments[-1].image.name}' for {end - start:.2f}s more.")
                segments[-1].end = end
            else:
                print(f"Warning: no image for image_id {beat['image_id']}; the next image will start at {start:.2f}s instead.")
                carry_start = start
            continue

        segments.append(Segment(image_id=beat["image_id"], image=image, start=start, end=end, animation=beat["animation"]))

    if not segments:
        raise EditorError("no images matched beat.json.")
    return segments


def assign_frames(segments: List[Segment], fps: int) -> None:
    """Cumulative rounding: the sum of all frames equals round(total * fps)."""
    for seg in segments:
        seg.frames = max(1, round(seg.end * fps) - round(seg.start * fps))


def choose_animations(segments: List[Segment], options: RenderOptions) -> None:
    previous = None
    for seg in segments:
        chosen = seg.animation or options.animation
        if chosen == "random":
            pool = [a for a in options.random_pool if a != previous] or list(options.random_pool)
            chosen = options.rng.choice(pool)
        seg.animation = chosen
        previous = chosen


# --------------------------------------------------------------------------- #
# FFmpeg expression helpers
# --------------------------------------------------------------------------- #


def fmt(value: float) -> str:
    text = f"{value:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def ease_expr(progress: str, easing: str) -> str:
    """Map a 0..1 expression through an easing curve (ffmpeg expression syntax)."""
    p = f"({progress})"
    if easing == "linear":
        return p
    if easing == "ease_in":
        return f"pow({p},2)"
    if easing == "ease_out":
        return f"(1-pow(1-{p},2))"
    return f"if(lt({p},0.5),2*pow({p},2),1-pow(-2*{p}+2,2)/2)"


def fit_filter(width: int, height: int, fit: str, bg_color: str) -> str:
    if fit == "contain":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color={bg_color}"
        )
    return f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}"


def zoompan_exprs(animation: str, progress: str, zoom: float, rng: random.Random) -> Tuple[str, str, str]:
    """Return (z, x, y) zoompan expressions for a motion preset."""
    center_x = "iw/2-(iw/zoom/2)"
    center_y = "ih/2-(ih/zoom/2)"
    span_x = "(iw-iw/zoom)"
    span_y = "(ih-ih/zoom)"
    z_in = f"1+{fmt(zoom)}*{progress}"
    z_out = f"1+{fmt(zoom)}*(1-{progress})"
    z_const = fmt(1 + zoom)

    if animation == "zoom_in":
        return z_in, center_x, center_y
    if animation == "zoom_out":
        return z_out, center_x, center_y
    if animation == "pan_left":
        return z_const, f"{span_x}*(1-{progress})", center_y
    if animation == "pan_right":
        return z_const, f"{span_x}*{progress}", center_y
    if animation == "pan_up":
        return z_const, center_x, f"{span_y}*(1-{progress})"
    if animation == "pan_down":
        return z_const, center_x, f"{span_y}*{progress}"
    if animation == "ken_burns":
        anchor_x, anchor_y = rng.choice(KEN_BURNS_ANCHORS)
        return z_in, f"{span_x}*{fmt(anchor_x)}", f"{span_y}*{fmt(anchor_y)}"
    if animation == "drift":
        return (
            f"1+{fmt(zoom)}*(0.5+0.5*{progress})",
            f"{span_x}*(0.5+0.3*sin(PI*{progress}))",
            f"{span_y}*(0.5-0.3*sin(PI*{progress}))",
        )
    return "1", center_x, center_y  # static presets


def segment_filter(index: int, seg: Segment, opt: RenderOptions) -> str:
    """Filter chain(s) turning input `index` into a [v<index>] stream of seg.frames frames."""
    width, height, fps = opt.width, opt.height, opt.fps
    frames = seg.frames
    duration = frames / fps
    denominator = max(frames - 1, 1)
    progress = ease_expr(f"min(on/{denominator},1)", opt.easing)
    animation = seg.animation or "none"

    if animation in SLIDE_ANIMATIONS:
        z, x, y = "1", "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    else:
        z, x, y = zoompan_exprs(animation, progress, opt.zoom, opt.rng)

    head = (
        f"[{index}:v]{fit_filter(width * UPSCALE, height * UPSCALE, opt.fit, opt.bg_color)},setsar=1,"
        f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={width}x{height}:fps={fps}"
    )

    post = [f"trim=end_frame={frames}", "setpts=PTS-STARTPTS"]
    transition = opt.transition
    if animation == "fadein" and transition == "none":
        transition = "fadein"
    elif animation == "fade":
        transition = "fade"
    if transition in ("fadein", "fade"):
        fade = min(opt.fade_duration, duration / 2)
        if fade >= 0.02:
            post.append(f"fade=t=in:st=0:d={fmt(fade)}")
            if transition == "fade":
                post.append(f"fade=t=out:st={fmt(duration - fade)}:d={fmt(fade)}")
    post.append("format=yuv420p")

    if animation not in SLIDE_ANIMATIONS:
        return f"{head},{','.join(post)}[v{index}]"

    slide = max(0.05, min(opt.slide_duration, duration))
    eased = ease_expr(f"min(t/{fmt(slide)},1)", opt.easing)
    ox, oy = "0", "0"
    if animation == "slide_left":
        ox = f"-main_w*(1-{eased})"
    elif animation == "slide_right":
        ox = f"main_w*(1-{eased})"
    elif animation == "slide_up":
        oy = f"main_h*(1-{eased})"
    elif animation == "slide_down":
        oy = f"-main_h*(1-{eased})"

    return ";\n".join([
        f"{head}[s{index}]",
        f"color=c={opt.bg_color}:s={width}x{height}:r={fps}:d={fmt(duration)}[b{index}]",
        f"[b{index}][s{index}]overlay=x='{ox}':y='{oy}':shortest=1,{','.join(post)}[v{index}]",
    ])


def build_filter_complex(segments: List[Segment], opt: RenderOptions) -> str:
    parts = [segment_filter(i, seg, opt) for i, seg in enumerate(segments)]
    inputs = "".join(f"[v{i}]" for i in range(len(segments)))
    parts.append(f"{inputs}concat=n={len(segments)}:v=1:a=0,format=yuv420p[outv]")
    return ";\n".join(parts) + "\n"


def build_command(segments: List[Segment], opt: RenderOptions, output: Path, filter_script: Path) -> List[str]:
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    for seg in segments:
        # One still per input; zoompan turns it into seg.frames frames.
        command += ["-i", str(seg.image)]
    command += [
        "-filter_complex_script", str(filter_script),
        "-map", "[outv]",
        "-c:v", "libx264",
        "-preset", opt.preset,
        "-crf", str(opt.crf),
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-r", str(opt.fps),
        str(output),
    ]
    return command


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def resolve_size(aspect: str, size: Optional[str]) -> Tuple[int, int]:
    if size:
        match = re.fullmatch(r"\s*(\d+)\s*[xX:]\s*(\d+)\s*", size)
        if not match:
            raise EditorError(f"--size must look like 1920x1080, got '{size}'")
        width, height = int(match.group(1)), int(match.group(2))
    else:
        width, height = ASPECT_PRESETS[aspect]
    width, height = max(2, width - width % 2), max(2, height - height % 2)  # libx264 needs even sizes
    return width, height


def parse_pool(raw: Optional[str]) -> Sequence[str]:
    if not raw:
        return DEFAULT_RANDOM_POOL
    pool = []
    for name in raw.split(","):
        name = name.strip().lower()
        if not name:
            continue
        if name == "random" or name not in ANIMATIONS:
            raise EditorError(f"--random-pool: unknown animation '{name}'")
        pool.append(name)
    if not pool:
        raise EditorError("--random-pool is empty")
    return tuple(pool)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create an animated video from sequential images using FFmpeg.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--imagesource", help="Directory containing the numbered images.")
    parser.add_argument("--beatpath", help="Path to beat.json.")
    parser.add_argument("--out", help="Output MP4 filename.")

    anim = parser.add_argument_group("animation")
    anim.add_argument("--animation", default="none", choices=ANIMATIONS, help="Animation preset for every scene.")
    anim.add_argument("--transition", default="none", choices=TRANSITIONS, help="Fade applied on top of the motion.")
    anim.add_argument("--smoothness", default="ease_in_out", choices=EASINGS, help="Easing curve of the motion.")
    anim.add_argument("--zoom", type=float, default=0.18, help="Motion strength: how far zoom/pan travels (0-1).")
    anim.add_argument("--fade-duration", type=float, default=0.6, help="Seconds for fade in/out.")
    anim.add_argument("--slide-duration", type=float, default=0.6, help="Seconds a slide-in takes.")
    anim.add_argument("--random-pool", default=None, help="Comma-separated presets 'random' may pick from.")
    anim.add_argument("--seed", type=int, default=None, help="Seed for random choices (printed when omitted).")

    frame = parser.add_argument_group("frame")
    frame.add_argument("--aspect", default="16:9", choices=sorted(ASPECT_PRESETS), help="Target aspect ratio.")
    frame.add_argument("--size", default=None, help="Explicit WxH output size (overrides --aspect).")
    frame.add_argument("--fit", default="cover", choices=FIT_MODES, help="cover crops, contain letterboxes.")
    frame.add_argument("--bg-color", default="black", help="Background for letterboxing and slide-ins.")
    frame.add_argument("--fps", type=int, default=30, help="Output FPS.")

    enc = parser.add_argument_group("encoding")
    enc.add_argument("--preset", default="medium", help="libx264 preset.")
    enc.add_argument("--crf", type=int, default=18, help="libx264 CRF quality (lower = better).")

    misc = parser.add_argument_group("misc")
    misc.add_argument("--on-missing", default="hold", choices=ON_MISSING,
                      help="What to do when a beat has no image: hold the previous image, skip the time, or fail.")
    misc.add_argument("--keep-filter-script", action="store_true", help="Keep the generated filter graph file.")
    misc.add_argument("--dry-run", action="store_true", help="Print the plan and the ffmpeg command, render nothing.")
    misc.add_argument("--list-animations", action="store_true", help="List animation presets and exit.")
    return parser


def print_timeline(segments: List[Segment], fps: int) -> None:
    print("Timeline:")
    print()
    for seg in segments:
        print(
            f"  {seg.image.name:<22} {seg.start:8.3f}s -> {seg.end:8.3f}s "
            f"({seg.duration:6.3f}s, {seg.frames:4d}f)  {seg.animation}"
        )
    print()


def run(args: argparse.Namespace) -> int:
    if args.list_animations:
        for name in ANIMATIONS:
            print(f"  {name:<12} {ANIMATION_HELP.get(name, '')}")
        return 0

    for required in ("imagesource", "beatpath", "out"):
        if not getattr(args, required):
            raise EditorError(f"--{required} is required")
    if args.fps <= 0:
        raise EditorError("--fps must be greater than 0.")
    if not 0 <= args.zoom <= 1:
        raise EditorError("--zoom must be between 0 and 1.")

    check_ffmpeg()

    imagesource = Path(args.imagesource).expanduser().resolve()
    beatpath = Path(args.beatpath).expanduser().resolve()
    output = Path(args.out).expanduser().resolve()
    width, height = resolve_size(args.aspect, args.size)

    seed = args.seed if args.seed is not None else random.randrange(1, 2**31)
    options = RenderOptions(
        width=width, height=height, fps=args.fps,
        animation=args.animation, transition=args.transition, easing=args.smoothness,
        fit=args.fit, zoom=args.zoom, fade_duration=max(0.0, args.fade_duration),
        slide_duration=max(0.05, args.slide_duration), bg_color=args.bg_color,
        random_pool=parse_pool(args.random_pool), rng=random.Random(seed),
        preset=args.preset, crf=args.crf,
    )

    images = find_images(imagesource)
    beats = load_beat_file(beatpath)
    segments = build_timeline(beats, images, args.on_missing)
    assign_frames(segments, args.fps)
    choose_animations(segments, options)
    total_duration = sum(seg.frames for seg in segments) / args.fps

    print()
    print("========================================")
    print("       Python FFmpeg Video Editor")
    print("========================================")
    print(f"Image source : {imagesource}")
    print(f"Beat file    : {beatpath}")
    print(f"Images found : {len(images)}")
    print(f"Scenes       : {len(segments)} of {len(beats)} beats")
    print(f"Frame        : {width}x{height} ({args.size or args.aspect}, fit={args.fit}) @ {args.fps} fps")
    print(f"Animation    : {args.animation}  transition={args.transition}  smoothness={args.smoothness}  zoom={args.zoom}")
    if args.animation == "random" or any(b["animation"] == "random" for b in beats):
        print(f"Random seed  : {seed}  (pass --seed {seed} to reproduce)")
    print(f"Output       : {output}")
    print("========================================")
    print()
    print_timeline(segments, args.fps)
    print(f"Video duration : {total_duration:.3f}s")
    print()

    output.parent.mkdir(parents=True, exist_ok=True)
    filter_script = output.with_name(f"{output.stem}.filter.txt")
    filter_graph = build_filter_complex(segments, options)
    command = build_command(segments, options, output, filter_script)

    if args.dry_run:
        print("Filter graph:")
        print(filter_graph)
        print("Command:")
        print(" ".join(command))
        return 0

    filter_script.write_text(filter_graph, encoding="utf-8")
    print("Starting FFmpeg...")
    print()
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        print()
        print(f"FFmpeg failed with exit code {exc.returncode}.")
        print(f"The filter graph was kept for inspection: {filter_script}")
        return exc.returncode or 1
    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        return 130
    finally:
        if not args.keep_filter_script and filter_script.exists() and output.exists():
            try:
                filter_script.unlink()
            except OSError:
                pass

    print()
    print("========================================")
    print("Video successfully created!")
    print(f"Output   : {output}")
    print(f"Duration : {total_duration:.3f}s")
    print("========================================")
    return 0


def main() -> int:
    args = build_parser().parse_args()
    try:
        return run(args)
    except EditorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
