#!/usr/bin/env python3

"""
Beat Alignment Agent

Reads:
    beat.md
    subtitle.srt

Uses an OpenAI-compatible LLM to align each beat/image with subtitle timing.
If the model keeps returning unusable output the beats are distributed
proportionally to their word count instead, so the pipeline never stalls here.

Output:
    beat.json  - continuous timeline (first image starts at 0, no gaps)

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
    MAX_TOKENS=8000
    TIMEOUT=120
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional

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
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8000"))
TIMEOUT = float(os.getenv("TIMEOUT", "120"))


class AlignmentError(Exception):
    """The model's answer was unusable (retryable)."""


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
    return ""


def timestamp_to_seconds(timestamp: str) -> float:
    """00:01:23,456 / 00:01:23.456 / 01:23.456 -> seconds."""
    timestamp = timestamp.strip().replace(",", ".")
    parts = timestamp.split(":")

    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(timestamp)
    except ValueError:
        raise ValueError(f"Invalid timestamp: {timestamp}")


def seconds_to_timestamp(seconds: float) -> str:
    """seconds -> HH:MM:SS.mmm"""
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

        if match_index > 0:
            try:
                subtitle_index = int(lines[match_index - 1])
            except ValueError:
                subtitle_index = len(subtitles) + 1
        else:
            subtitle_index = len(subtitles) + 1

        text = " ".join(lines[match_index + 1:])
        text = re.sub(r"<[^>]+>", "", text)
        text = re.sub(r"\{[^}]+\}", "", text)

        subtitles.append(
            {
                "index": subtitle_index,
                "start": timestamp_to_seconds(match.group("start")),
                "end": timestamp_to_seconds(match.group("end")),
                "text": text.strip(),
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
    Parse common beat.md formats:

        beat1 → narration          (what script-gen writes)
        beat 1 -> narration
        Image 1: narration
        # Image 1 / ## 1 heading followed by a description
        1. narration
        blank-line separated paragraphs (fallback)
    """
    beat_text = beat_text.replace("\r\n", "\n").replace("\r", "\n").strip()

    if not beat_text:
        die("Beat markdown file is empty.")

    beats: list[dict[str, Any]] = []

    # Format 1: "beat1 → text", "beat 12 -> text", "Image 3: text", "scene 2 - text"
    labelled_pattern = re.compile(
        r"(?mi)^\s*(?:image|img|beat|scene|shot)\s*#?\s*(\d+)\s*(?:\u2192|->|=>|:|-|\u2013|\u2014)\s*(.+?)\s*$"
    )
    labelled = list(labelled_pattern.finditer(beat_text))
    if labelled:
        for match in labelled:
            beats.append({"image_id": int(match.group(1)), "text": match.group(2).strip()})
        return _dedupe(beats)

    # Format 2: markdown headings "# Image 1" / "## 1" followed by a description
    heading_pattern = re.compile(
        r"(?m)^\s*#{1,6}\s*(?:image|img|beat|scene|shot)?\s*(\d+)\s*[:\-]?\s*$",
        re.IGNORECASE,
    )
    matches = list(heading_pattern.finditer(beat_text))
    if matches:
        for i, match in enumerate(matches):
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(beat_text)
            beats.append({"image_id": int(match.group(1)), "text": beat_text[start:end].strip()})
        return _dedupe(beats)

    # Format 3: numbered list "1. text"
    numbered_pattern = re.compile(r"(?m)^\s*(\d+)[.)]\s+(.+?)\s*$")
    numbered = list(numbered_pattern.finditer(beat_text))
    if numbered:
        for match in numbered:
            beats.append({"image_id": int(match.group(1)), "text": match.group(2).strip()})
        return _dedupe(beats)

    # Fallback: each non-empty paragraph is an image.
    for image_id, paragraph in enumerate(re.split(r"\n\s*\n", beat_text), start=1):
        paragraph = paragraph.strip()
        if paragraph:
            beats.append({"image_id": image_id, "text": paragraph})

    if not beats:
        die("Could not identify any beats in beat.md.")
    return beats


def _dedupe(beats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    unique = []
    for beat in beats:
        if beat["image_id"] in seen:
            print(f"WARNING: duplicate beat id {beat['image_id']} in beat.md, keeping the first one.")
            continue
        seen.add(beat["image_id"])
        unique.append(beat)
    unique.sort(key=lambda b: b["image_id"])
    return unique


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

    return OpenAI(api_key=API_KEY, base_url=BASE_URL, timeout=TIMEOUT, max_retries=2)


def build_prompt(
    beats: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
    feedback: Optional[str] = None,
) -> str:
    beat_section = [f"IMAGE {beat['image_id']}\n{beat['text']}" for beat in beats]
    subtitle_section = [
        f"[{s['index']}] {seconds_to_timestamp(s['start'])} --> {seconds_to_timestamp(s['end'])}\n{s['text']}"
        for s in subtitles
    ]

    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n\nYOUR PREVIOUS ANSWER WAS REJECTED because: "
            f"{feedback}\nFix that and return only the corrected JSON array.\n"
        )

    return f"""
You are a subtitle-to-visual-beat alignment agent.

Your task is to determine when each image/beat should appear in a video.

You have:

1. IMAGE/BEAT DESCRIPTIONS ({len(beats)} images)
2. SUBTITLE TIMELINE

Each image represents a visual beat. The beat text is the narration that is
spoken while the image is on screen, so match it against the subtitle text.

Choose a continuous start and end time for every image.

IMPORTANT RULES:

- Every image must appear exactly once: return exactly {len(beats)} entries.
- Preserve image_id exactly.
- Use only timing information supported by the subtitle timeline.
- An image may cover multiple subtitle entries.
- Multiple images may occur during the same subtitle.
- Do not create new image IDs.
- Do not remove image IDs.
- Keep the image sequence in ascending image_id order.
- The first image starts at the beginning of the first subtitle.
- The last image ends at the end of the final subtitle.
- No gaps between images: each start_time equals the previous end_time.
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

{chr(10).join(subtitle_section)}{feedback_block}
""".strip()


def call_llm(client: OpenAI, prompt: str, max_tokens: int) -> list[dict[str, Any]]:
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
            {"role": "user", "content": prompt},
        ],
        temperature=TEMPERATURE,
        max_tokens=max_tokens,
    )

    if not response.choices:
        raise AlignmentError("the model returned no choices")
    content = (response.choices[0].message.content or "").strip()
    if not content:
        raise AlignmentError("the model returned an empty response")

    # Tolerate markdown fences and prose around the array.
    content = re.sub(r"^```(?:json)?\s*", "", content, flags=re.IGNORECASE)
    content = re.sub(r"\s*```$", "", content, flags=re.IGNORECASE)
    candidates = [content]
    first, last = content.find("["), content.rfind("]")
    if first != -1 and last > first:
        candidates.append(content[first:last + 1])

    parsed = None
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            break
        except json.JSONDecodeError:
            continue

    if parsed is None:
        print("\nMODEL RESPONSE (unparseable):\n")
        print(content[:2000])
        raise AlignmentError("the response was not valid JSON (possibly truncated - raise MAX_TOKENS)")

    if isinstance(parsed, dict):
        for key in ("beats", "images", "items", "result", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if not isinstance(parsed, list):
        raise AlignmentError("output must be a JSON array")
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
        raise AlignmentError(f"you returned {len(result)} entries but {len(beats)} images were expected")

    cleaned: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    video_start = subtitles[0]["start"]
    video_end = subtitles[-1]["end"]

    for item in result:
        if not isinstance(item, dict):
            raise AlignmentError("each output item must be an object")
        if not {"image_id", "start_time", "end_time"}.issubset(item):
            raise AlignmentError("an item is missing image_id, start_time or end_time")

        try:
            image_id = int(item["image_id"])
            start_time = float(item["start_time"])
            end_time = float(item["end_time"])
        except (ValueError, TypeError):
            raise AlignmentError("image_id, start_time and end_time must be numeric")

        if image_id not in expected_set:
            raise AlignmentError(f"unexpected image_id {image_id}")
        if image_id in seen_ids:
            raise AlignmentError(f"duplicate image_id {image_id}")
        if start_time < 0:
            raise AlignmentError(f"image_id {image_id}: start_time cannot be negative")
        if end_time <= start_time:
            raise AlignmentError(f"image_id {image_id}: end_time must be greater than start_time")

        start_time = max(video_start, start_time)
        end_time = min(video_end, end_time)
        if end_time <= start_time:
            raise AlignmentError(f"image_id {image_id}: timing lies outside the subtitle duration")

        cleaned.append({"image_id": image_id, "start_time": round(start_time, 3), "end_time": round(end_time, 3)})
        seen_ids.add(image_id)

    missing = expected_set - seen_ids
    if missing:
        raise AlignmentError(f"missing image IDs: {sorted(missing)}")

    cleaned.sort(key=lambda x: x["image_id"])
    return cleaned


# ---------------------------------------------------------------------------
# TIMELINE CLEANUP
# ---------------------------------------------------------------------------

def smooth_timings(result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split accidental overlaps between neighbours at their midpoint."""
    if len(result) < 2:
        return result

    result = [dict(item) for item in result]
    for i in range(len(result) - 1):
        current, following = result[i], result[i + 1]
        if current["end_time"] > following["start_time"]:
            midpoint = round((current["end_time"] + following["start_time"]) / 2, 3)
            if current["start_time"] < midpoint < following["end_time"]:
                current["end_time"] = midpoint
                following["start_time"] = midpoint
    return result


def fill_gaps(result: list[dict[str, Any]], video_end: float) -> list[dict[str, Any]]:
    """Make the timeline continuous: start at 0, each image ends where the next starts."""
    if not result:
        return result

    result = [dict(item) for item in result]
    result[0]["start_time"] = 0.0
    for i in range(len(result) - 1):
        following_start = result[i + 1]["start_time"]
        if following_start > result[i]["start_time"]:
            result[i]["end_time"] = following_start
    result[-1]["end_time"] = max(result[-1]["end_time"], round(video_end, 3))
    return result


def proportional_alignment(
    beats: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Fallback: share the spoken duration between beats by word count."""
    video_start = subtitles[0]["start"]
    video_end = subtitles[-1]["end"]
    total = max(0.1, video_end - video_start)
    weights = [max(1, len(beat["text"].split())) for beat in beats]
    weight_sum = sum(weights)

    result = []
    cursor = video_start
    for beat, weight in zip(beats, weights):
        length = total * weight / weight_sum
        result.append({"image_id": beat["image_id"], "start_time": round(cursor, 3), "end_time": round(cursor + length, 3)})
        cursor += length
    result[-1]["end_time"] = round(video_end, 3)
    return result


# ---------------------------------------------------------------------------
# ALIGN WITH RETRIES
# ---------------------------------------------------------------------------

def align(
    client: OpenAI,
    beats: list[dict[str, Any]],
    subtitles: list[dict[str, Any]],
    retries: int,
    max_tokens: int,
) -> list[dict[str, Any]]:
    feedback = None
    last_error: Optional[Exception] = None
    attempts = retries + 1

    for attempt in range(1, attempts + 1):
        print(f"Sending alignment task to model (attempt {attempt}/{attempts})...")
        try:
            raw = call_llm(client, build_prompt(beats, subtitles, feedback), max_tokens)
            return validate_result(raw, beats, subtitles)
        except AlignmentError as exc:
            last_error = exc
            feedback = str(exc)
            print(f"  rejected: {exc}")
        except Exception as exc:  # network / API errors
            last_error = exc
            print(f"  failed: {exc}")

        if attempt < attempts:
            delay = min(30, 2 ** attempt)
            print(f"  retrying in {delay}s...")
            time.sleep(delay)

    raise AlignmentError(str(last_error) if last_error else "alignment failed")


# ---------------------------------------------------------------------------
# SAVE
# ---------------------------------------------------------------------------

def save_json(output_path: str, data: list[dict[str, Any]]) -> None:
    path = Path(output_path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        die(f"Could not write output file: {exc}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Align beat.md images with subtitle.srt timing using an OpenAI-compatible LLM."
    )
    parser.add_argument("--beatsource", required=True, help="Path to beat.md")
    parser.add_argument("--srtsource", required=True, help="Path to subtitle.srt")
    parser.add_argument("--out", default="beat.json", help="Output JSON path (default: beat.json)")
    parser.add_argument("--retries", type=int, default=2,
                        help="How often to re-ask the model after an unusable answer (default: 2)")
    parser.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                        help=f"Max tokens for the model answer (default: {MAX_TOKENS}, env MAX_TOKENS)")
    parser.add_argument("--no-fallback", action="store_true",
                        help="Fail instead of falling back to a proportional alignment when the model gives up")
    parser.add_argument("--keep-gaps", action="store_true",
                        help="Keep the model's gaps instead of producing a continuous timeline")
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

    beats = parse_beats(read_text(args.beatsource))
    subtitles = parse_srt(read_text(args.srtsource))

    print(f"Detected images    : {len(beats)}")
    print(f"Detected subtitles : {len(subtitles)}")
    print(f"Video duration     : {seconds_to_timestamp(subtitles[-1]['end'])}")
    print()

    client = create_client()
    print(f"Using model: {MODEL_NAME}")
    print(f"Base URL: {BASE_URL}")

    source = "model"
    try:
        result = align(client, beats, subtitles, retries=max(0, args.retries), max_tokens=args.max_tokens)
    except AlignmentError as exc:
        if args.no_fallback:
            die(f"Alignment failed: {exc}")
        print()
        print(f"WARNING: the model could not produce a valid alignment ({exc}).")
        print("WARNING: falling back to a proportional word-count alignment.")
        result = proportional_alignment(beats, subtitles)
        source = "proportional fallback"

    result = smooth_timings(result)
    if not args.keep_gaps:
        result = fill_gaps(result, subtitles[-1]["end"])

    save_json(args.out, result)

    print()
    print(f"Alignment complete ({source}).")
    print(f"Saved: {args.out}")
    print("\nPreview:")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
