
#!/usr/bin/env python3

"""
Beat Alignment Agent

Reads:
    beat.md
    subtitle.srt

Uses an OpenAI-compatible LLM to align each beat/image with subtitle timing.

Output:
    beat.json

Example:
    python3 beatalign.py \
        --beatsource "../../beat.md" \
        --srtsource "../../subtitle.srt" \
        --out beat.json

Environment variables (.env):
    MODEL_NAME=your-model-name
    API_KEY=your-api-key
    BASE_URL=https://api.openai.com/v1

Optional:
    TEMPERATURE=0.0
    MAX_TOKENS=4000
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI


# ---------------------------------------------------------------------------
# ENV
# ---------------------------------------------------------------------------

load_dotenv()

MODEL_NAME = os.getenv("MODEL_NAME", "").strip()
API_KEY = os.getenv("API_KEY", "").strip()
BASE_URL = os.getenv("BASE_URL", "https://api.openai.com/v1").strip()

TEMPERATURE = float(os.getenv("TEMPERATURE", "0.0"))
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "4000"))


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def die(message: str, code: int = 1) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    sys.exit(code)


def read_text(path: str) -> str:
    file_path = Path(path)

    if not file_path.exists():
        die(f"File not found: {file_path}")

    try:
        return file_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        die(f"Could not read UTF-8 text file: {file_path}")


def timestamp_to_seconds(timestamp: str) -> float:
    """
    Convert:
        00:01:23,456
        00:01:23.456
        01:23.456

    into seconds.
    """
    timestamp = timestamp.strip().replace(",", ".")

    parts = timestamp.split(":")

    try:
        if len(parts) == 3:
            hours = float(parts[0])
            minutes = float(parts[1])
            seconds = float(parts[2])
            return hours * 3600 + minutes * 60 + seconds

        if len(parts) == 2:
            minutes = float(parts[0])
            seconds = float(parts[1])
            return minutes * 60 + seconds

        return float(timestamp)

    except ValueError:
        raise ValueError(f"Invalid timestamp: {timestamp}")


def seconds_to_timestamp(seconds: float) -> str:
    """
    Convert seconds to:
        HH:MM:SS.mmm
    """
    milliseconds = round(seconds * 1000)

    hours = milliseconds // 3_600_000
    milliseconds %= 3_600_000

    minutes = milliseconds // 60_000
    milliseconds %= 60_000

    secs = milliseconds // 1000
    milliseconds %= 1000

    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


# ---------------------------------------------------------------------------
# SRT PARSER
# ---------------------------------------------------------------------------

def parse_srt(srt_text: str) -> list[dict[str, Any]]:
    """
    Parse SRT into:

    [
        {
            "index": 1,
            "start": 0.0,
            "end": 2.35,
            "text": "Hello..."
        }
    ]
    """

    srt_text = srt_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not srt_text:
        die("Subtitle file is empty.")

    blocks = re.split(r"\n\s*\n", srt_text)

    subtitles: list[dict[str, Any]] = []

    timestamp_pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
        r"\s*-->\s*"
        r"(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    )

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]

        if not lines:
            continue

        match = None
        match_index = -1

        for i, line in enumerate(lines):
            candidate = timestamp_pattern.search(line)
            if candidate:
                match = candidate
                match_index = i
                break

        if not match:
            continue

        subtitle_index = 0

        if match_index > 0:
            try:
                subtitle_index = int(lines[match_index - 1])
            except ValueError:
                subtitle_index = len(subtitles) + 1
        else:
            subtitle_index = len(subtitles) + 1

        text_lines = lines[match_index + 1:]

        text = " ".join(text_lines)

        # Remove basic SRT styling tags.
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\{[^}]+\}", "", text)

        subtitles.append(
            {
                "index": subtitle_index,
                "start": timestamp_to_seconds(match.group("start")),
                "end": timestamp_to_seconds(match.group("end")),
                "text": text,
            }
        )

    if not subtitles:
        die("No valid subtitles were found in the SRT file.")

    subtitles.sort(key=lambda x: x["start"])

    return subtitles


# ---------------------------------------------------------------------------
# BEAT PARSER
# ---------------------------------------------------------------------------

def parse_beats(beat_text: str) -> list[dict[str, Any]]:
    """
    Parse common beat.md formats.

    Supported examples:

        # Image 1
        A monk walking through a forest

        # Image 2
        Close-up of the monk

    or:

        ## 1
        ...

        ## 2
        ...

    or:

        Image 1: ...
        Image 2: ...

    The parser keeps the original text so the LLM can interpret it.
    """

    beat_text = beat_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not beat_text:
        die("Beat markdown file is empty.")

    beats: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Format 1:
    #   # Image 1
    #   description
    #
    # Format 2:
    #   ## 1
    #   description
    # ------------------------------------------------------------------

    heading_pattern = re.compile(
        r"(?m)^\s*#{1,6}\s*"
        r"(?:image|img|beat|scene)?\s*"
        r"(\d+)"
        r"\s*[:\-]?\s*$",
        re.IGNORECASE,
    )

    matches = list(heading_pattern.finditer(beat_text))

    if matches:
        for i, match in enumerate(matches):
            image_id = int(match.group(1))

            start = match.end()

            if i + 1 < len(matches):
                end = matches[i + 1].start()
            else:
                end = len(beat_text)

            description = beat_text[start:end].strip()

            beats.append(
                {
                    "image_id": image_id,
                    "text": description,
                }
            )

        return beats

    # ------------------------------------------------------------------
    # Format 3:
    #
    # Image 1: description
    # Image 2: description
    # ------------------------------------------------------------------

    line_pattern = re.compile(
        r"(?mi)^\s*(?:image|img|beat|scene)\s*"
        r"(\d+)\s*:\s*(.+?)\s*$"
    )

    line_matches = list(line_pattern.finditer(beat_text))

    if line_matches:
        for match in line_matches:
            beats.append(
                {
                    "image_id": int(match.group(1)),
                    "text": match.group(2).strip(),
                }
            )

        return beats

    # ------------------------------------------------------------------
    # Format 4:
    #
    # 1. description
    # 2. description
    # ------------------------------------------------------------------

    numbered_pattern = re.compile(
        r"(?m)^\s*(\d+)\.\s+(.+?)\s*$"
    )

    numbered_matches = list(numbered_pattern.finditer(beat_text))

    if numbered_matches:
        for match in numbered_matches:
            beats.append(
                {
                    "image_id": int(match.group(1)),
                    "text": match.group(2).strip(),
                }
            )

        return beats

    # ------------------------------------------------------------------
    # Fallback:
    # Treat each non-empty paragraph as an image.
    # ------------------------------------------------------------------

    paragraphs = re.split(r"\n\s*\n", beat_text)

    image_id = 1

    for paragraph in paragraphs:
        paragraph = paragraph.strip()

        if not paragraph:
            continue

        beats.append(
            {
                "image_id": image_id,
                "text": paragraph,
            }
        )

        image_id += 1

    if not beats:
        die("Could not identify any beats in beat.md.")

    return beats


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

def create_client() -> OpenAI:
    if not MODEL_NAME:
        die("MODEL_NAME is missing from .env")

    if not API_KEY:
        die("API_KEY is missing from .env")

    if not BASE_URL:
        die("BASE_URL is missing from .env")

    return OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL,
    )


def build_prompt(
    beats: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
) -> str:

    beat_section = []

    for beat in beats:
        beat_section.append(
            f"IMAGE {beat['image_id']}\n"
            f"{beat['text']}"
        )

    subtitle_section = []

    for subtitle in subtitles:
        subtitle_section.append(
            f"[{subtitle['index']}] "
            f"{seconds_to_timestamp(subtitle['start'])}"
            f" --> "
            f"{seconds_to_timestamp(subtitle['end'])}\n"
            f"{subtitle['text']}"
        )

    return f"""
You are a subtitle-to-visual-beat alignment agent.

Your task is to determine when each image/beat should appear in a video.

You have:

1. IMAGE/BEAT DESCRIPTIONS
2. SUBTITLE TIMELINE

Each image represents a visual beat.

Choose a continuous start and end time for every image.

IMPORTANT RULES:

- Every image must appear exactly once.
- Preserve image_id exactly.
- Use only timing information supported by the subtitle timeline.
- An image may cover multiple subtitle entries.
- Multiple images may occur during the same subtitle.
- Do not create new image IDs.
- Do not remove image IDs.
- Keep the image sequence in ascending image_id order.
- The first image should normally start at or near the beginning of the first subtitle.
- The last image should normally end at the end of the final subtitle.
- Avoid unnecessary gaps between images.
- Avoid overlaps unless there is a strong narrative reason.
- Prefer clean transitions aligned with natural sentence/scene changes.
- Use decimal seconds.
- start_time and end_time must be numbers, not strings.
- end_time must always be greater than start_time.

Return ONLY valid JSON.

Required format:

[
  {{
    "image_id": 1,
    "start_time": 0.0,
    "end_time": 4.25
  }},
  {{
    "image_id": 2,
    "start_time": 4.25,
    "end_time": 9.80
  }}
]

IMAGE/BEAT DESCRIPTIONS:

{chr(10).join(beat_section)}

SUBTITLE TIMELINE:

{chr(10).join(subtitle_section)}
""".strip()


def call_llm(
    client: OpenAI,
    prompt: str,
) -> list[dict[str, Any]]:

    print(f"Using model: {MODEL_NAME}")
    print(f"Base URL: {BASE_URL}")

    response = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a precise video-editing alignment agent. "
                    "Return machine-readable JSON only."
                ),
            },
            {
                "role": "user",
                "content": prompt,
            },
        ],
        temperature=TEMPERATURE,
        max_tokens=MAX_TOKENS,
    )

    content = response.choices[0].message.content

    if not content:
        die("The model returned an empty response.")

    content = content.strip()

    # Handle accidental markdown fences.
    content = re.sub(
        r"^```(?:json)?\s*",
        "",
        content,
        flags=re.IGNORECASE,
    )

    content = re.sub(
        r"\s*```$",
        "",
        content,
        flags=re.IGNORECASE,
    )

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        print("\nMODEL RESPONSE:\n")
        print(content)
        die(f"Model returned invalid JSON: {exc}")

    if not isinstance(parsed, list):
        die("Model output must be a JSON array.")

    return parsed


# ---------------------------------------------------------------------------
# VALIDATION
# ---------------------------------------------------------------------------

def validate_result(
    result: list[dict[str, Any]],
    beats: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
) -> list[dict[str, Any]]:

    expected_ids = [beat["image_id"] for beat in beats]
    expected_set = set(expected_ids)

    if len(result) != len(beats):
        die(
            f"Model returned {len(result)} entries, "
            f"but {len(beats)} images were expected."
        )

    cleaned: list[dict[str, Any]] = []

    seen_ids: set[int] = set()

    video_start = subtitles[0]["start"]
    video_end = subtitles[-1]["end"]

    for item in result:
        if not isinstance(item, dict):
            die("Each output item must be an object.")

        required = {"image_id", "start_time", "end_time"}

        if not required.issubset(item):
            die(
                "Output item is missing fields. "
                "Required: image_id, start_time, end_time"
            )

        try:
            image_id = int(item["image_id"])
            start_time = float(item["start_time"])
            end_time = float(item["end_time"])
        except (ValueError, TypeError):
            die("image_id, start_time and end_time must be numeric.")

        if image_id not in expected_set:
            die(f"Unexpected image_id: {image_id}")

        if image_id in seen_ids:
            die(f"Duplicate image_id: {image_id}")

        if start_time < 0:
            die(f"image_id {image_id}: start_time cannot be negative.")

        if end_time <= start_time:
            die(
                f"image_id {image_id}: "
                "end_time must be greater than start_time."
            )

        # Keep output inside actual subtitle duration.
        start_time = max(video_start, start_time)
        end_time = min(video_end, end_time)

        if end_time <= start_time:
            die(
                f"image_id {image_id}: timing became invalid "
                "after clamping to subtitle duration."
            )

        cleaned.append(
            {
                "image_id": image_id,
                "start_time": round(start_time, 3),
                "end_time": round(end_time, 3),
            }
        )

        seen_ids.add(image_id)

    missing = expected_set - seen_ids

    if missing:
        die(f"Missing image IDs: {sorted(missing)}")

    # Force deterministic image order.
    cleaned.sort(key=lambda x: x["image_id"])

    return cleaned


# ---------------------------------------------------------------------------
# OPTIONAL SMOOTHING
# ---------------------------------------------------------------------------

def smooth_timings(
    result: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Prevent small accidental overlaps caused by model output.

    When two sequential images overlap, split at their midpoint.

    Example:

        image 1: 0 -> 5
        image 2: 4 -> 9

    becomes approximately:

        image 1: 0 -> 4.5
        image 2: 4.5 -> 9
    """

    if len(result) < 2:
        return result

    result = [dict(item) for item in result]

    for i in range(len(result) - 1):
        current = result[i]
        next_item = result[i + 1]

        if current["end_time"] > next_item["start_time"]:
            midpoint = round(
                (current["end_time"] + next_item["start_time"]) / 2,
                3,
            )

            if midpoint > current["start_time"] and midpoint < next_item["end_time"]:
                current["end_time"] = midpoint
                next_item["start_time"] = midpoint

    return result


# ---------------------------------------------------------------------------
# SAVE
# ---------------------------------------------------------------------------

def save_json(
    output_path: str,
    data: list[dict[str, Any]],
) -> None:

    path = Path(output_path)

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        path.write_text(
            json.dumps(
                data,
                indent=2,
                ensure_ascii=False,
            ) + "\n",
            encoding="utf-8",
        )

    except OSError as exc:
        die(f"Could not write output file: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align beat.md images with subtitle.srt timing using an OpenAI-compatible LLM."
    )

    parser.add_argument(
        "--beatsource",
        required=True,
        help="Path to beat.md",
    )

    parser.add_argument(
        "--srtsource",
        required=True,
        help="Path to subtitle.srt",
    )

    parser.add_argument(
        "--out",
        default="beat.json",
        help="Output JSON path (default: beat.json)",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main() -> None:

    args = parse_args()

    print("=" * 60)
    print("Beat Alignment Agent")
    print("=" * 60)

    print(f"Beat source : {args.beatsource}")
    print(f"SRT source  : {args.srtsource}")
    print(f"Output      : {args.out}")
    print()

    # Read files.
    beat_text = read_text(args.beatsource)
    srt_text = read_text(args.srtsource)

    # Parse.
    beats = parse_beats(beat_text)
    subtitles = parse_srt(srt_text)

    print(f"Detected images    : {len(beats)}")
    print(f"Detected subtitles : {len(subtitles)}")
    print(
        f"Video duration     : "
        f"{seconds_to_timestamp(subtitles[-1]['end'])}"
    )
    print()

    # LLM client.
    client = create_client()

    prompt = build_prompt(
        beats=beats,
        subtitles=subtitles,
    )

    print("Sending alignment task to model...")
    print()

    result = call_llm(
        client=client,
        prompt=prompt,
    )

    result = validate_result(
        result=result,
        beats=beats,
        subtitles=subtitles,
    )

    result = smooth_timings(result)

    save_json(
        output_path=args.out,
        data=result,
    )

    print("Alignment complete.")
    print(f"Saved: {args.out}")

    print("\nPreview:")
    print(
        json.dumps(
            result,
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

