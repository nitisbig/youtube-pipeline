#!/usr/bin/env python3

import argparse
import subprocess
import sys
from pathlib import Path


def add_audio(video_path, audio_path, output_path):
    video_path = Path(video_path)
    audio_path = Path(audio_path)
    output_path = Path(output_path)

    if not video_path.exists():
        print(f"Error: Video not found: {video_path}")
        sys.exit(1)

    if not audio_path.exists():
        print(f"Error: Audio not found: {audio_path}")
        sys.exit(1)

    command = [
        "ffmpeg",
        "-y",
        "-i", str(video_path),
        "-i", str(audio_path),

        # Copy video without re-encoding
        "-c:v", "copy",

        # Encode audio
        "-c:a", "aac",
        "-b:a", "192k",

        # Stop when the shortest input ends
        "-shortest",

        str(output_path),
    ]

    print("Adding audio...")
    print(" ".join(command))

    try:
        subprocess.run(command, check=True)
        print(f"\nDone: {output_path}")
    except subprocess.CalledProcessError:
        print("\nFFmpeg failed.")
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Add an audio track to a video using FFmpeg."
    )

    parser.add_argument(
        "--video",
        required=True,
        help="Path to input video"
    )

    parser.add_argument(
        "--audio",
        required=True,
        help="Path to audio file"
    )

    parser.add_argument(
        "--out",
        required=True,
        help="Path to output video"
    )

    args = parser.parse_args()

    add_audio(
        args.video,
        args.audio,
        args.out
    )


if __name__ == "__main__":
    main()