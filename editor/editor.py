#!/usr/bin/env python3

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}


def natural_sort_key(path: Path):
    """
    Natural filename sorting:
    01.png
    02.png
    10.png
    """
    return [
        int(part) if part.isdigit() else part.lower()
        for part in re.split(r"(\d+)", path.name)
    ]


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("Error: ffmpeg is not installed.")
        print("Install it with:")
        print("  sudo apt install ffmpeg")
        sys.exit(1)


def find_images(source: Path):
    if not source.exists():
        print(f"Error: image directory does not exist:\n{source}")
        sys.exit(1)

    if not source.is_dir():
        print(f"Error: not a directory:\n{source}")
        sys.exit(1)

    images = [
        file
        for file in source.iterdir()
        if file.is_file() and file.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    images.sort(key=natural_sort_key)

    if not images:
        print(f"Error: no images found in:\n{source}")
        sys.exit(1)

    return images


def load_beat_file(beat_path: Path):
    """
    Load beat.json.

    Expected format:

    [
        {
            "image_id": 1,
            "start_time": 0,
            "end_time": 3.5
        },
        ...
    ]
    """

    if not beat_path.exists():
        print(f"Error: beat file does not exist:\n{beat_path}")
        sys.exit(1)

    try:
        with beat_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON in beat file:\n{e}")
        sys.exit(1)

    if not isinstance(data, list):
        print("Error: beat.json must contain a JSON array.")
        sys.exit(1)

    beats = {}

    for item in data:
        if not isinstance(item, dict):
            print("Error: every beat entry must be an object.")
            sys.exit(1)

        required = {
            "image_id",
            "start_time",
            "end_time",
        }

        missing = required - item.keys()

        if missing:
            print(
                f"Error: beat entry is missing fields: "
                f"{', '.join(sorted(missing))}"
            )
            sys.exit(1)

        try:
            image_id = int(item["image_id"])
            start_time = float(item["start_time"])
            end_time = float(item["end_time"])
        except (TypeError, ValueError):
            print(f"Error: invalid beat entry: {item}")
            sys.exit(1)

        if end_time <= start_time:
            print(
                f"Error: image_id {image_id} has invalid timing: "
                f"{start_time} -> {end_time}"
            )
            sys.exit(1)

        beats[image_id] = {
            "start_time": start_time,
            "end_time": end_time,
            "duration": end_time - start_time,
        }

    return beats


def get_image_id(image: Path):
    """
    Extract image ID from filename.

    Examples:
        01.png -> 1
        02.jpg -> 2
        100.webp -> 100
    """

    match = re.match(r"^(\d+)", image.stem)

    if not match:
        return None

    return int(match.group(1))


def build_filter(images, beats, animation, fps):
    """
    Build FFmpeg filter_complex.

    Each image gets its own duration from beat.json.
    """

    filters = []
    valid_images = []

    for index, image in enumerate(images):
        image_id = get_image_id(image)

        if image_id is None:
            print(
                f"Warning: cannot determine image_id from "
                f"filename '{image.name}', skipping."
            )
            continue

        if image_id not in beats:
            print(
                f"Warning: no beat entry for image_id={image_id} "
                f"({image.name}), skipping."
            )
            continue

        duration = beats[image_id]["duration"]

        input_label = f"{index}:v"
        output_label = f"v{len(valid_images)}"

        base = (
            f"[{input_label}]"
            f"scale=1920:1080:"
            f"force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2,"
            f"setsar=1,"
            f"fps={fps}"
        )

        if animation == "fadein":
            # Fade-in duration cannot be longer than the image itself.
            fade_duration = min(0.7, duration)

            video_filter = (
                f"{base},"
                f"fade=t=in:st=0:d={fade_duration},"
                f"trim=duration={duration},"
                f"setpts=PTS-STARTPTS"
            )

        elif animation == "none":
            video_filter = (
                f"{base},"
                f"trim=duration={duration},"
                f"setpts=PTS-STARTPTS"
            )

        else:
            raise ValueError(
                f"Unsupported animation: {animation}"
            )

        filters.append(
            f"{video_filter}[{output_label}]"
        )

        valid_images.append(image)

    if not valid_images:
        print("Error: no images matched beat.json.")
        sys.exit(1)

    concat_inputs = "".join(
        f"[v{i}]" for i in range(len(valid_images))
    )

    filters.append(
        f"{concat_inputs}"
        f"concat=n={len(valid_images)}:v=1:a=0,"
        f"format=yuv420p"
        f"[outv]"
    )

    return ";".join(filters), valid_images


def build_command(images, beats, animation, output, fps):
    """
    Build complete FFmpeg command.

    Important:
    Every image receives its own duration from beat.json.
    """

    command = ["ffmpeg", "-y"]

    valid_images = []

    for image in images:
        image_id = get_image_id(image)

        if image_id is None or image_id not in beats:
            continue

        duration = beats[image_id]["duration"]

        command.extend(
            [
                "-loop",
                "1",
                "-t",
                str(duration),
                "-i",
                str(image),
            ]
        )

        valid_images.append(image)

    filter_complex, valid_images = build_filter(
        images=images,
        beats=beats,
        animation=animation,
        fps=fps,
    )

    command.extend(
        [
            "-filter_complex",
            filter_complex,
            "-map",
            "[outv]",
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            "-r",
            str(fps),
            str(output),
        ]
    )

    return command, valid_images


def main():

    parser = argparse.ArgumentParser(
        description="Create a video from sequential images using FFmpeg."
    )

    parser.add_argument(
        "--imagesource",
        required=True,
        help="Directory containing the images.",
    )

    parser.add_argument(
        "--beatpath",
        required=True,
        help="Path to beat.json.",
    )

    parser.add_argument(
        "--animation",
        default="none",
        choices=[
            "none",
            "fadein",
        ],
        help="Image animation.",
    )

    parser.add_argument(
        "--out",
        required=True,
        help="Output MP4 filename.",
    )

    parser.add_argument(
        "--fps",
        type=int,
        default=30,
        help="Output FPS. Default: 30.",
    )

    args = parser.parse_args()

    check_ffmpeg()

    imagesource = Path(args.imagesource).expanduser().resolve()
    beatpath = Path(args.beatpath).expanduser().resolve()
    output = Path(args.out).expanduser().resolve()

    if args.fps <= 0:
        print("Error: FPS must be greater than 0.")
        sys.exit(1)

    # ---------------------------------------------------------
    # Load files
    # ---------------------------------------------------------

    images = find_images(imagesource)
    beats = load_beat_file(beatpath)

    # ---------------------------------------------------------
    # Display timeline
    # ---------------------------------------------------------

    print()
    print("========================================")
    print("       Python FFmpeg Video Editor")
    print("========================================")
    print(f"Image source : {imagesource}")
    print(f"Beat file    : {beatpath}")
    print(f"Images found : {len(images)}")
    print(f"Animation    : {args.animation}")
    print(f"FPS          : {args.fps}")
    print(f"Output       : {output}")
    print("========================================")
    print()

    total_duration = 0.0
    matched = 0

    print("Timeline:")
    print()

    for image in images:

        image_id = get_image_id(image)

        if image_id is None:
            continue

        if image_id not in beats:
            print(
                f"  {image.name:<20} "
                f"NO BEAT"
            )
            continue

        beat = beats[image_id]

        start = beat["start_time"]
        end = beat["end_time"]
        duration = beat["duration"]

        total_duration += duration
        matched += 1

        print(
            f"  {image.name:<20} "
            f"{start:8.3f}s -> "
            f"{end:8.3f}s "
            f"({duration:.3f}s)"
        )

    print()
    print(f"Matched images : {matched}")
    print(f"Video duration  : {total_duration:.3f}s")
    print()

    if matched == 0:
        print("Error: no images matched beat.json.")
        sys.exit(1)

    # ---------------------------------------------------------
    # Build FFmpeg command
    # ---------------------------------------------------------

    command, valid_images = build_command(
        images=images,
        beats=beats,
        animation=args.animation,
        output=output,
        fps=args.fps,
    )

    print("Starting FFmpeg...")
    print()

    try:
        subprocess.run(command, check=True)

    except subprocess.CalledProcessError as e:
        print()
        print("FFmpeg failed.")
        print(f"Exit code: {e.returncode}")
        sys.exit(e.returncode)

    except KeyboardInterrupt:
        print()
        print("Cancelled.")
        sys.exit(1)

    print()
    print("========================================")
    print("Video successfully created!")
    print(f"Output : {output}")
    print(f"Duration : {total_duration:.3f}s")
    print("========================================")


if __name__ == "__main__":
    main()