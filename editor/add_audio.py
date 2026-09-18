#!/usr/bin/env python3
"""
add_audio.py - mux the narration onto the rendered video.

By default the video stream is copied (fast, lossless) and the output stops
with the shorter input. With --extend-video the last frame is held until the
audio ends instead (this needs a re-encode, and only happens when the audio
really is longer), so a narration that runs a little past the picture is never
cut off.
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"Error: {name} is not installed. Install it with: sudo apt install ffmpeg")


def probe_duration(path: Path):
    """Duration in seconds via ffprobe, or None if it can't be determined."""
    if shutil.which("ffprobe") is None:
        return None
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def build_command(video: Path, audio: Path, output: Path, bitrate: str, pad_seconds: float,
                  audio_fade_out: float, audio_duration):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
               "-i", str(video), "-i", str(audio)]

    if pad_seconds > 0.05:
        # Hold the last frame so the picture lasts as long as the narration.
        command += [
            "-filter_complex", f"[0:v]tpad=stop_mode=clone:stop_duration={pad_seconds:.3f}[v]",
            "-map", "[v]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
        ]
    else:
        command += ["-map", "0:v:0", "-c:v", "copy"]

    command += ["-map", "1:a:0", "-c:a", "aac", "-b:a", bitrate]
    if audio_fade_out > 0 and audio_duration:
        start = max(0.0, audio_duration - audio_fade_out)
        command += ["-af", f"afade=t=out:st={start:.3f}:d={audio_fade_out:.3f}"]

    command += ["-shortest", "-movflags", "+faststart", str(output)]
    return command


def add_audio(video_path, audio_path, output_path, bitrate="192k", extend_video=False, audio_fade_out=0.0):
    video = Path(video_path).expanduser()
    audio = Path(audio_path).expanduser()
    output = Path(output_path).expanduser()

    if not video.exists():
        sys.exit(f"Error: video not found: {video}")
    if not audio.exists():
        sys.exit(f"Error: audio not found: {audio}")
    check_tool("ffmpeg")

    video_duration = probe_duration(video)
    audio_duration = probe_duration(audio)
    pad_seconds = 0.0
    if video_duration is not None and audio_duration is not None:
        print(f"Video duration : {video_duration:.3f}s")
        print(f"Audio duration : {audio_duration:.3f}s")
        diff = audio_duration - video_duration
        if diff > 0.05:
            if extend_video:
                pad_seconds = diff
                print(f"Audio is {diff:.2f}s longer - holding the last frame to match (re-encoding video).")
            else:
                print(f"Warning: audio is {diff:.2f}s longer than the video and will be cut (use --extend-video).")
        elif diff < -0.05:
            print(f"Note: video is {-diff:.2f}s longer than the audio; the output ends with the audio.")

    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(video, audio, output, bitrate, pad_seconds, audio_fade_out, audio_duration)

    print("Adding audio...")
    print(" ".join(command))
    try:
        subprocess.run(command, check=True)
    except subprocess.CalledProcessError as exc:
        sys.exit(f"\nFFmpeg failed with exit code {exc.returncode}.")
    except KeyboardInterrupt:
        sys.exit("\nCancelled.")
    print(f"\nDone: {output}")


def main():
    parser = argparse.ArgumentParser(description="Add an audio track to a video using FFmpeg.")
    parser.add_argument("--video", required=True, help="Path to input video")
    parser.add_argument("--audio", required=True, help="Path to audio file")
    parser.add_argument("--out", required=True, help="Path to output video")
    parser.add_argument("--bitrate", default="192k", help="AAC bitrate (default 192k)")
    parser.add_argument("--extend-video", action="store_true",
                        help="Hold the last frame until the audio ends instead of cutting the audio.")
    parser.add_argument("--audio-fade-out", type=float, default=0.0,
                        help="Fade the audio out over the last N seconds (0 = off).")
    args = parser.parse_args()

    add_audio(args.video, args.audio, args.out, args.bitrate, args.extend_video, args.audio_fade_out)


if __name__ == "__main__":
    main()
