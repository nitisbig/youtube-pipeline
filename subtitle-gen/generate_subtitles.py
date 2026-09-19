#!/usr/bin/env python3
"""
generate_subtitles.py - Transcribe audio into SRT subtitles using whisper.cpp (whisper-cli).

Supports GPU acceleration with seamless automatic fallback to CPU.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def str2bool(val) -> bool:
    """Parse boolean from CLI arguments or configuration."""
    if isinstance(val, bool):
        return val
    if val is None:
        return False
    v = str(val).strip().lower()
    if v in ("yes", "true", "t", "y", "1"):
        return True
    if v in ("no", "false", "f", "n", "0", ""):
        return False
    raise argparse.ArgumentTypeError(f"Boolean value expected, got '{val}'.")


def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"Error: {name} is not installed or not in PATH.")


def generate_subtitles(
    audio_path: Path,
    out_prefix: Path,
    model_path: Path,
    cli_path: Path,
    use_gpu: bool = False,
    device: int = 0,
    threads: int = 4,
) -> int:
    audio = Path(audio_path).resolve()
    model = Path(model_path).resolve()
    cli = Path(cli_path).resolve()

    if not audio.exists():
        sys.exit(f"Error: Audio file not found: {audio}")
    if not model.exists():
        sys.exit(f"Error: Whisper model file not found: {model}")
    if not cli.exists():
        sys.exit(f"Error: whisper-cli binary not found: {cli}")

    check_tool("ffmpeg")

    # If out_prefix ends with .srt, strip it because whisper-cli appends .srt automatically
    out_str = str(out_prefix)
    if out_str.endswith(".srt"):
        out_str = out_str[:-4]
    out_prefix = Path(out_str).resolve()
    out_prefix.parent.mkdir(parents=True, exist_ok=True)

    # Convert audio to 16kHz mono PCM WAV required by whisper.cpp
    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp_wav.close()
    tmp_wav_path = Path(tmp_wav.name)

    try:
        print(f"Converting {audio.name} to 16kHz mono WAV...")
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(audio),
            "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le",
            str(tmp_wav_path),
        ]
        subprocess.run(ffmpeg_cmd, check=True)

        base_cmd = [
            str(cli),
            "-m", str(model),
            "-f", str(tmp_wav_path),
            "-osrt",
            "-of", str(out_prefix),
            "-t", str(threads),
        ]

        if use_gpu:
            gpu_cmd = base_cmd + ["-dev", str(device)]
            print(f"Transcribing audio with whisper-cli (GPU requested, device {device})...")
            try:
                res = subprocess.run(gpu_cmd)
                if res.returncode == 0:
                    print(f"Subtitles generated successfully: {out_prefix}.srt")
                    return 0
                print(
                    f"\n[WARNING] whisper-cli GPU execution returned code {res.returncode}. "
                    "Falling back to CPU (--no-gpu)...",
                    file=sys.stderr,
                )
            except Exception as exc:
                print(
                    f"\n[WARNING] whisper-cli GPU execution failed: {exc}. "
                    "Falling back to CPU (--no-gpu)...",
                    file=sys.stderr,
                )

        # CPU execution
        cpu_cmd = base_cmd + ["-ng"]
        print("Transcribing audio with whisper-cli (CPU)...")
        res = subprocess.run(cpu_cmd)
        if res.returncode != 0:
            sys.exit(f"\nwhisper-cli failed with exit code {res.returncode}")

        print(f"Subtitles generated successfully: {out_prefix}.srt")
        return 0

    finally:
        if tmp_wav_path.exists():
            try:
                tmp_wav_path.unlink()
            except OSError:
                pass


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Transcribe audio to SRT subtitles using whisper.cpp with GPU/CPU support.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--audio", "-a", required=True, help="Path to input audio file")
    parser.add_argument("--out", "-o", required=True, help="Output prefix or SRT path")
    parser.add_argument("--model", "-m", default="models/ggml-tiny.en.bin", help="Path to whisper model binary")
    parser.add_argument("--cli-path", default="./build/bin/whisper-cli", help="Path to whisper-cli binary")
    parser.add_argument(
        "--gpu",
        nargs="?",
        const=True,
        default=False,
        type=str2bool,
        help="Use GPU acceleration if supported, with automatic CPU fallback.",
    )
    parser.add_argument("--device", type=int, default=0, help="GPU device ID")
    parser.add_argument("--threads", type=int, default=4, help="Number of compute threads")

    args = parser.parse_args()

    sys.exit(
        generate_subtitles(
            audio_path=Path(args.audio),
            out_prefix=Path(args.out),
            model_path=Path(args.model),
            cli_path=Path(args.cli_path),
            use_gpu=args.gpu,
            device=args.device,
            threads=args.threads,
        )
    )


if __name__ == "__main__":
    main()
