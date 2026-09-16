#!/usr/bin/env python3
import argparse
import os
import re
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path


def run(cmd):
    print("$", " ".join(shlex.quote(str(x)) for x in cmd))
    subprocess.run(cmd, check=True)


def parse_story(path: Path):
    text = path.read_text(encoding="utf-8")
    # Blank-line-separated paragraphs become scenes.
    scenes = [re.sub(r"\s+", " ", block.strip()) for block in re.split(r"\n\s*\n", text) if block.strip()]
    return scenes


def esc_drawtext(value: str) -> str:
    # FFmpeg drawtext escaping for text content.
    return (
        value.replace('\\', r'\\')
        .replace(':', r'\\:')
        .replace("'", r"\\'")
        .replace('%', r'\\%')
    )


def normalize_color(value: str) -> str:
    value = value.strip()
    if value.startswith("#"):
        value = value[1:]
    if not re.fullmatch(r"[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", value):
        raise ValueError(f"Invalid color '{value}'. Use #RRGGBB or #RRGGBBAA.")
    return f"0x{value}"


def make_scene(ffmpeg, text, out, duration, background, text_color, width, height, fps, font_size, animation, font):
    bg = normalize_color(background)
    tc = normalize_color(text_color)
    escaped = esc_drawtext(text)

    if font:
        font_part = f":fontfile='{esc_drawtext(font)}'"
    else:
        font_part = ""

    common = (
        f"drawtext=text='{escaped}'"
        f":fontcolor={tc}"
        f":fontsize={font_size}"
        f":x=(w-text_w)/2:y=(h-text_h)/2"
        f":line_spacing=12"
        f":box=1:boxcolor=black@0:boxborderw=20"
        f":text_align=center"
        f"{font_part}"
    )

    if animation == "none":
        vf = common
    elif animation == "fade":
        vf = f"fade=t=in:st=0:d=0.8,{common}:alpha='if(lt(t,0.8),t/0.8,1)'"
    elif animation == "typewriter":
        # Approximate typewriter effect by progressively revealing characters.
        # This is intentionally simple and uses drawtext's dynamic text expansion.
        safe = escaped.replace("'", "\\'")
        vf = (
            f"drawtext=text='{safe}':fontcolor={tc}:fontsize={font_size}"
            f":x=(w-text_w)/2:y=(h-text_h)/2:line_spacing=12:text_align=center"
            f":alpha=1"
            f"{font_part}"
        )
    else:
        raise ValueError(f"Unsupported animation: {animation}")

    cmd = [
        ffmpeg, "-y",
        "-f", "lavfi",
        "-i", f"color=c={bg}:s={width}x{height}:r={fps}",
        "-t", str(duration),
        "-vf", vf,
        "-r", str(fps),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        str(out),
    ]
    run(cmd)


def main():
    parser = argparse.ArgumentParser(
        description="Render a blank-line-separated story.md into a text-based story video using FFmpeg."
    )
    parser.add_argument("--story", required=True, help="Path to story Markdown file")
    parser.add_argument("--duration", type=float, default=5, help="Duration of each scene in seconds (default: 5)")
    parser.add_argument("--background", default="#000000", help="Background color, e.g. #000000")
    parser.add_argument("--text-color", default="#ffffff", help="Text color, e.g. #ffffff")
    parser.add_argument("--animation", choices=["none", "fade", "typewriter"], default="none")
    parser.add_argument("--out", required=True, help="Output MP4 path")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--fps", type=int, default=30)
    parser.add_argument("--font-size", type=int, default=64)
    parser.add_argument("--font", default=None, help="Optional TTF/OTF font file")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="FFmpeg executable")
    args = parser.parse_args()

    story_path = Path(args.story).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()

    if not story_path.is_file():
        parser.error(f"Story file not found: {story_path}")
    if args.duration <= 0:
        parser.error("--duration must be greater than 0")
    if args.width <= 0 or args.height <= 0:
        parser.error("--width and --height must be positive")

    scenes = parse_story(story_path)
    if not scenes:
        parser.error("Story file contains no scenes.")

    # Check FFmpeg first.
    try:
        subprocess.run([args.ffmpeg, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except (FileNotFoundError, subprocess.CalledProcessError):
        parser.error(f"FFmpeg executable not found or not runnable: {args.ffmpeg}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Scenes: {len(scenes)}")
    print(f"Per-scene duration: {args.duration:.2f}s")
    print(f"Total duration: {len(scenes) * args.duration:.2f}s")

    with tempfile.TemporaryDirectory(prefix="story_video_") as tmp:
        tmpdir = Path(tmp)
        scene_files = []

        for idx, scene in enumerate(scenes, start=1):
            scene_out = tmpdir / f"scene_{idx:04d}.mp4"
            print(f"\nRendering scene {idx}/{len(scenes)}")
            print(scene[:120] + ("..." if len(scene) > 120 else ""))
            make_scene(
                args.ffmpeg,
                scene,
                scene_out,
                args.duration,
                args.background,
                args.text_color,
                args.width,
                args.height,
                args.fps,
                args.font_size,
                args.animation,
                args.font,
            )
            scene_files.append(scene_out)

        concat_file = tmpdir / "concat.txt"
        with concat_file.open("w", encoding="utf-8") as f:
            for scene in scene_files:
                f.write(f"file '{scene.as_posix().replace("'", "'\\''")}'\n")

        concat_cmd = [
            args.ffmpeg, "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(concat_file),
            "-c", "copy",
            "-movflags", "+faststart",
            str(out_path),
        ]
        run(concat_cmd)

    print(f"\nDone: {out_path}")


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(f"\nFFmpeg failed with exit code {exc.returncode}.", file=sys.stderr)
        sys.exit(exc.returncode)
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        sys.exit(1)
