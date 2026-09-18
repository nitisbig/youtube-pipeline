#!/usr/bin/env python3
"""
Local AI voice enhancer for YouTube/documentary narration.

Uses FFmpeg only:
  - high-pass filter
  - gentle voice EQ
  - compression
  - de-esser
  - EBU R128 loudness normalization
  - true-peak limiting through loudnorm

Examples:
    uv run enhancer.py input.wav
    uv run enhancer.py input.wav -o output.wav
    uv run enhancer.py input.wav --preset clean
    uv run enhancer.py input.wav --target-lufs -14

FFmpeg must be installed and available in PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


PRESETS = {
    "youtube": {
        "description": "Clear, warm, loud documentary narration",
        "highpass": 70,
        "lowpass": 17000,
        "eq": [
            "equalizer=f=120:t=q:w=0.9:g=-1.5",
            "equalizer=f=280:t=q:w=1.0:g=-1.5",
            "equalizer=f=3200:t=q:w=0.9:g=2.2",
            "equalizer=f=6500:t=q:w=0.9:g=1.0",
        ],
        "compression": "compand=attacks=0.005:decays=0.08:points=-80/-80|-18/-18|0/-6",
        "deesser": "deesser=i=0.25:m=0.45:f=0.5",
        "target_lufs": -16.0,
        "true_peak": -1.5,
        "lra": 7.0,
    },
    "clean": {
        "description": "Gentler processing for already-clean AI speech",
        "highpass": 65,
        "lowpass": 18000,
        "eq": [
            "equalizer=f=150:t=q:w=0.9:g=-1.0",
            "equalizer=f=3000:t=q:w=1.0:g=1.5",
            "equalizer=f=7000:t=q:w=0.9:g=0.5",
        ],
        "compression": "compand=attacks=0.005:decays=0.10:points=-80/-80|-20/-20|0/-5",
        "deesser": "deesser=i=0.18:m=0.35:f=0.5",
        "target_lufs": -16.0,
        "true_peak": -1.5,
        "lra": 7.0,
    },
    "strong": {
        "description": "More controlled, dense narration",
        "highpass": 75,
        "lowpass": 16500,
        "eq": [
            "equalizer=f=120:t=q:w=0.9:g=-2.0",
            "equalizer=f=250:t=q:w=1.0:g=-2.0",
            "equalizer=f=3000:t=q:w=0.9:g=2.8",
            "equalizer=f=7000:t=q:w=0.8:g=1.2",
        ],
        "compression": "compand=attacks=0.003:decays=0.07:points=-80/-80|-22/-22|-12/-9|0/-5",
        "deesser": "deesser=i=0.35:m=0.55:f=0.45",
        "target_lufs": -15.0,
        "true_peak": -1.2,
        "lra": 6.0,
    },
}


def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(code)


def require_ffmpeg() -> str:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        die(
            "FFmpeg was not found in PATH. Install it with:\n"
            "  sudo apt update && sudo apt install ffmpeg"
        )
    return ffmpeg


def run_command(cmd: list[str], capture_output: bool = False) -> subprocess.CompletedProcess[str]:
    print("$", " ".join(cmd))
    return subprocess.run(
        cmd,
        text=True,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        check=False,
    )


def build_base_filters(preset: dict, use_deesser: bool) -> str:
    filters = [
        f"highpass=f={preset['highpass']}",
        *preset["eq"],
        preset["compression"],
    ]

    if use_deesser:
        filters.append(preset["deesser"])

    filters.append(f"lowpass=f={preset['lowpass']}")
    return ",".join(filters)


def parse_loudnorm_json(stderr: str) -> dict:
    # loudnorm prints a JSON object near the end of stderr.
    matches = re.findall(r"\{\s*\"input_i\".*?\}", stderr, flags=re.DOTALL)
    if not matches:
        # Fall back to the last balanced-looking JSON block.
        starts = [m.start() for m in re.finditer(r"\{\s*\"input_i\"", stderr)]
        if starts:
            candidate = stderr[starts[-1]:]
            end = candidate.find("}")
            if end != -1:
                matches = [candidate[: end + 1]]

    if not matches:
        raise ValueError("Could not find loudnorm measurement JSON in FFmpeg output.")

    data = json.loads(matches[-1])

    required = [
        "input_i",
        "input_tp",
        "input_lra",
        "input_thresh",
        "target_offset",
    ]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(f"loudnorm JSON is missing fields: {', '.join(missing)}")

    return data


def measure(
    ffmpeg: str,
    input_file: Path,
    base_filters: str,
    target_lufs: float,
    true_peak: float,
    lra: float,
) -> dict:
    filtergraph = (
        f"{base_filters},"
        f"loudnorm=I={target_lufs}:TP={true_peak}:LRA={lra}:print_format=json"
    )

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-i",
        str(input_file),
        "-vn",
        "-af",
        filtergraph,
        "-f",
        "null",
        "-",
    ]

    result = run_command(cmd, capture_output=True)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        die("FFmpeg failed during loudness measurement.")

    try:
        return parse_loudnorm_json(result.stderr)
    except (ValueError, json.JSONDecodeError) as exc:
        print(result.stderr[-5000:], file=sys.stderr)
        die(f"Could not parse FFmpeg loudness measurement: {exc}")


def make_loudnorm_filter(
    measurement: dict,
    target_lufs: float,
    true_peak: float,
    lra: float,
) -> str:
    return (
        f"loudnorm="
        f"I={target_lufs}:"
        f"TP={true_peak}:"
        f"LRA={lra}:"
        f"measured_I={measurement['input_i']}:"
        f"measured_TP={measurement['input_tp']}:"
        f"measured_LRA={measurement['input_lra']}:"
        f"measured_thresh={measurement['input_thresh']}:"
        f"offset={measurement['target_offset']}:"
        f"linear=true:"
        f"print_format=summary"
    )


def output_arguments(output_file: Path) -> list[str]:
    ext = output_file.suffix.lower()

    if ext == ".wav":
        return ["-c:a", "pcm_s24le"]
    if ext == ".flac":
        return ["-c:a", "flac"]
    if ext in {".mp3", ".mp2"}:
        return ["-c:a", "libmp3lame", "-b:a", "320k"]

    die(
        f"Unsupported output format '{ext}'. Use .wav, .flac, or .mp3."
    )
    return []


def enhance(
    input_file: Path,
    output_file: Path,
    preset_name: str,
    target_lufs: float | None,
    true_peak: float | None,
    lra: float | None,
    use_deesser: bool,
) -> None:
    ffmpeg = require_ffmpeg()

    if not input_file.exists():
        die(f"Input file does not exist: {input_file}")

    if input_file.resolve() == output_file.resolve():
        die("Input and output must be different files.")

    preset = PRESETS[preset_name]

    target_lufs = preset["target_lufs"] if target_lufs is None else target_lufs
    true_peak = preset["true_peak"] if true_peak is None else true_peak
    lra = preset["lra"] if lra is None else lra

    if not (-70 <= target_lufs <= -5):
        die("--target-lufs must be between -70 and -5.")
    if not (-9 <= true_peak <= 0):
        die("--true-peak must be between -9 and 0.")
    if not (1 <= lra <= 50):
        die("--lra must be between 1 and 50.")

    output_file.parent.mkdir(parents=True, exist_ok=True)

    print()
    print(f"Preset       : {preset_name}")
    print(f"Input        : {input_file}")
    print(f"Output       : {output_file}")
    print(f"Target LUFS  : {target_lufs}")
    print(f"True peak    : {true_peak} dBTP")
    print(f"Target LRA   : {lra} LU")
    print(f"De-esser     : {'on' if use_deesser else 'off'}")
    print()

    base_filters = build_base_filters(preset, use_deesser)

    print("Pass 1/2: measuring loudness...")
    measurement = measure(
        ffmpeg,
        input_file,
        base_filters,
        target_lufs,
        true_peak,
        lra,
    )

    print(
        "Measured:",
        f"I={measurement['input_i']} LUFS,",
        f"TP={measurement['input_tp']} dBTP,",
        f"LRA={measurement['input_lra']} LU",
    )

    loudnorm_filter = make_loudnorm_filter(
        measurement,
        target_lufs,
        true_peak,
        lra,
    )

    final_filter = f"{base_filters},{loudnorm_filter}"

    cmd = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(input_file),
        "-vn",
        "-af",
        final_filter,
        "-ar",
        "48000",
        *output_arguments(output_file),
        str(output_file),
    ]

    print()
    print("Pass 2/2: rendering enhanced audio...")
    result = run_command(cmd)

    if result.returncode != 0:
        die("FFmpeg failed while rendering the enhanced audio.")

    if not output_file.exists() or output_file.stat().st_size == 0:
        die("FFmpeg reported success, but the output file was not created.")

    print()
    print("Done.")
    print(f"Enhanced file: {output_file}")
    print(f"Size: {output_file.stat().st_size / 1024 / 1024:.2f} MB")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Enhance AI-generated narration locally using FFmpeg."
    )

    parser.add_argument(
        "input_positional",
        nargs="?",
        type=Path,
        help="Input audio file (.wav, .mp3, .flac, etc.)",
    )
    parser.add_argument(
        "--input",
        dest="input_option",
        type=Path,
        help="Input audio file (.wav, .mp3, .flac, etc.)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output file. Default: <input>_enhanced.wav",
    )
    parser.add_argument(
        "--preset",
        choices=sorted(PRESETS),
        default="youtube",
        help="Voice processing preset. Default: youtube",
    )
    parser.add_argument(
        "--target-lufs",
        type=float,
        default=None,
        help="Target integrated loudness. Default depends on preset.",
    )
    parser.add_argument(
        "--true-peak",
        type=float,
        default=None,
        help="Maximum true peak in dBTP. Default depends on preset.",
    )
    parser.add_argument(
        "--lra",
        type=float,
        default=None,
        help="Target loudness range. Default depends on preset.",
    )
    parser.add_argument(
        "--no-deesser",
        action="store_true",
        help="Disable the de-esser.",
    )
    parser.add_argument(
        "--list-presets",
        action="store_true",
        help="List available presets and exit.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.list_presets:
        for name, preset in PRESETS.items():
            print(f"{name:8} - {preset['description']}")
        return

    input_file = args.input_option or args.input_positional

    if input_file is None:
        parser.error("provide the input audio file either as a positional argument or with --input")

    if args.output:
        output_file = args.output
    else:
        output_file = input_file.with_name(
            f"{input_file.stem}_enhanced.wav"
        )

    enhance(
        input_file=input_file,
        output_file=output_file,
        preset_name=args.preset,
        target_lufs=args.target_lufs,
        true_peak=args.true_peak,
        lra=args.lra,
        use_deesser=not args.no_deesser,
    )


if __name__ == "__main__":
    main()
