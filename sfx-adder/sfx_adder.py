#!/usr/bin/env python3
"""
sfx_adder.py - Sound Effects Alignment & Audio Mixing Worker

Responsibilities:
1. Reads the video (final.mp4), subtitle cues (<slug>.srt), beat markers (beat.json),
   and voiceover text (voiceover.md).
2. Scans the sound effects library (library/ folder).
3. Adds a beat transition pop (soft-pop.mp3) for every beat cut.
4. Uses an OpenAI-compatible LLM (configured in .env via Groq/OpenAI) to analyze
   the voiceover narrative and decide contextually matching sound effects from
   the library, perfectly aligned to subtitle timestamps.
5. Provides an intelligent, deterministic keyword fallback if the LLM is unreachable.
6. Exports a complete timeline schedule (sfx_cues.json) for inspection and tuning.
7. Blends all sound effects into the video using FFmpeg (adelay + amix) with
   lossless video stream passthrough (-c:v copy) for ultra-fast, high-quality execution.

Example:
    python3 sfx_adder.py \\
        --video out/demo/final.mp4 \\
        --srt out/demo/demo.srt \\
        --beat out/demo/beat.json \\
        --voiceover out/demo/voiceover.md \\
        --library library \\
        --out out/demo/final_sfx.mp4 \\
        --cues-out out/demo/sfx_cues.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple


# --------------------------------------------------------------------------- #
# Environment & Configuration Loader (Zero External Dependencies)
# --------------------------------------------------------------------------- #

def load_env_file(dotenv_path: Path) -> Dict[str, str]:
    """Parse a simple .env file without requiring python-dotenv."""
    env_vars: Dict[str, str] = {}
    if not dotenv_path.is_file():
        return env_vars

    try:
        content = dotenv_path.read_text(encoding="utf-8")
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip("'\"")
                env_vars[key] = val
    except Exception as exc:
        print(f"[sfx-adder] Warning: Failed to parse {dotenv_path}: {exc}", file=sys.stderr)
    return env_vars


def get_env_config(script_dir: Path) -> Dict[str, str]:
    """Load configuration from .env in script directory or parent directory."""
    env = {}
    local_env = script_dir / ".env"
    if local_env.is_file():
        env.update(load_env_file(local_env))
    parent_env = script_dir.parent / ".env"
    if parent_env.is_file():
        for k, v in load_env_file(parent_env).items():
            if k not in env:
                env[k] = v

    # Fall back to os.environ
    for key in ("MODEL_NAME", "API_KEY", "BASE_URL", "TEMPERATURE", "TIMEOUT"):
        if key in os.environ and key not in env:
            env[key] = os.environ[key]

    return {
        "model_name": env.get("MODEL_NAME", "").strip(),
        "api_key": env.get("API_KEY", "").strip(),
        "base_url": env.get("BASE_URL", "https://api.openai.com/v1").strip().rstrip("/"),
        "temperature": env.get("TEMPERATURE", "0.0").strip(),
        "timeout": env.get("TIMEOUT", "30").strip(),
    }


# --------------------------------------------------------------------------- #
# Data Structures
# --------------------------------------------------------------------------- #

@dataclass
class SubtitleCue:
    index: int
    start: float
    end: float
    text: str


@dataclass
class SoundEffectFile:
    filename: str
    path: Path
    stem: str
    duration: float


@dataclass
class SFXEvent:
    time: float
    sfx_file: str
    sfx_path: str
    type: str  # "beat" or "semantic"
    volume: float
    reason: str
    cue_index: Optional[int] = None
    phrase: Optional[str] = None


# --------------------------------------------------------------------------- #
# Audio & Video Probing Utilities
# --------------------------------------------------------------------------- #

def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        print(f"Error: Required tool '{name}' is not installed or not in PATH.", file=sys.stderr)
        sys.exit(1)


def probe_duration(path: Path) -> Optional[float]:
    """Return media duration in seconds via ffprobe."""
    if shutil.which("ffprobe") is None or not path.exists():
        return None
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def probe_has_audio(video_path: Path) -> bool:
    """Check if the video has an existing audio stream."""
    if shutil.which("ffprobe") is None or not video_path.exists():
        return False
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_type",
        "-of", "default=noprint_wrappers=1:nokey=1", str(video_path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return res.stdout.strip().lower() == "audio"
    except (subprocess.CalledProcessError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Parsers: SRT, Beat JSON, Library
# --------------------------------------------------------------------------- #

def timestamp_to_seconds(ts: str) -> float:
    """Convert '00:01:23,456' or '00:01:23.456' to float seconds."""
    clean = ts.strip().replace(",", ".")
    parts = clean.split(":")
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def parse_srt(srt_path: Path) -> List[SubtitleCue]:
    """Parse an SRT subtitle file into SubtitleCue items."""
    if not srt_path.is_file():
        return []

    content = srt_path.read_text(encoding="utf-8", errors="replace")
    content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not content:
        return []

    pattern = re.compile(
        r"(?P<start>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})\s*-->\s*(?P<end>\d{1,2}:\d{2}:\d{2}[,.]\d{1,3})"
    )

    cues: List[SubtitleCue] = []
    blocks = re.split(r"\n\s*\n", content)

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue

        match = None
        match_idx = -1
        for i, line in enumerate(lines):
            m = pattern.search(line)
            if m:
                match = m
                match_idx = i
                break

        if not match:
            continue

        cue_idx = len(cues) + 1
        if match_idx > 0:
            try:
                cue_idx = int(lines[match_idx - 1])
            except ValueError:
                pass

        raw_text = " ".join(lines[match_idx + 1:])
        # Remove HTML and ASS styling tags
        clean_text = re.sub(r"<[^>]+>", "", raw_text)
        clean_text = re.sub(r"\{[^}]+\}", "", clean_text).strip()

        start_sec = timestamp_to_seconds(match.group("start"))
        end_sec = timestamp_to_seconds(match.group("end"))

        cues.append(SubtitleCue(index=cue_idx, start=start_sec, end=end_sec, text=clean_text))

    return cues


def load_beats(beat_path: Path) -> List[Dict[str, Any]]:
    """Load beat boundaries from beat.json or fallback to beat.md."""
    if not beat_path.is_file():
        return []

    if beat_path.suffix.lower() == ".json":
        try:
            data = json.loads(beat_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [b for b in data if isinstance(b, dict) and "start_time" in b]
        except Exception as exc:
            print(f"[sfx-adder] Warning: Failed to parse {beat_path}: {exc}", file=sys.stderr)
            return []

    # If markdown file, we only have beat cues without timestamps unless paired with srt
    return []


def scan_library(library_dir: Path) -> Dict[str, SoundEffectFile]:
    """Scan the library folder for all supported audio files."""
    valid_exts = {".mp3", ".wav", ".ogg", ".flac", ".m4a", ".aac"}
    library_files: Dict[str, SoundEffectFile] = {}

    if not library_dir.is_dir():
        return library_files

    for entry in sorted(library_dir.iterdir()):
        if entry.is_file() and entry.suffix.lower() in valid_exts:
            dur = probe_duration(entry) or 1.0
            library_files[entry.name] = SoundEffectFile(
                filename=entry.name,
                path=entry.resolve(),
                stem=entry.stem,
                duration=dur,
            )

    return library_files


# --------------------------------------------------------------------------- #
# LLM Semantic Alignment & Fallback Engine
# --------------------------------------------------------------------------- #

FALLBACK_KEYWORD_MAP: Dict[str, List[str]] = {
    "camera-flash": ["camera", "flash", "photo", "picture", "snapshot", "image", "frame", "mirror", "look"],
    "chutter-click": ["click", "shutter", "switch", "button", "press", "type", "tick", "clock", "lock"],
    "key-collect": ["key", "collect", "unlock", "secret", "reveal", "coin", "treasure", "solve", "door", "solution", "discovery"],
    "perkutut-bird": ["bird", "nature", "morning", "peace", "forest", "tree", "sing", "calm", "gentle", "outside"],
    "woosh": ["whoosh", "swoosh", "sudden", "fast", "speed", "rush", "sweep", "drift", "transition", "shift", "burst", "leak"],
    "acceptance": ["accept", "acceptance", "peace", "truth", "harmony", "realize", "realization", "understand", "clarity", "vulnerability"],
}


def call_openai_chat(
    base_url: str,
    api_key: str,
    model_name: str,
    messages: List[Dict[str, str]],
    temperature: float = 0.0,
    timeout: int = 30,
) -> Optional[str]:
    """Call OpenAI-compatible chat completion endpoint using stdlib urllib."""
    url = f"{base_url}/chat/completions"
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temperature,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": "YouTubePipeline-SFXAdder/1.0",
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            choices = data.get("choices", [])
            if choices and "message" in choices[0]:
                return choices[0]["message"].get("content", "").strip()
    except Exception as exc:
        print(f"[sfx-adder] LLM request failed: {exc}", file=sys.stderr)

    return None


def extract_json_block(text: str) -> Optional[Any]:
    """Extract JSON data from raw text or markdown fences."""
    text = text.strip()
    # Strip markdown code blocks
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    if fenced_match:
        text = fenced_match.group(1).strip()

    # Try direct parse
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try searching for [ ... ] array
    arr_match = re.search(r"(\[[\s\S]*\])", text)
    if arr_match:
        try:
            return json.loads(arr_match.group(1))
        except json.JSONDecodeError:
            pass

    return None


def query_llm_for_sfx(
    cues: List[SubtitleCue],
    voiceover_text: str,
    available_sfx: List[SoundEffectFile],
    config: Dict[str, str],
    max_cues_to_send: int = 50,
) -> List[Dict[str, Any]]:
    """Prompt the LLM to identify contextual sound effects."""
    if not config["api_key"] or not config["model_name"]:
        print("[sfx-adder] No LLM API key / model configured in .env. Using intelligent keyword fallback.", file=sys.stderr)
        return []

    # Prepare available sound effects list
    sfx_descriptions = []
    for sfx in available_sfx:
        sfx_descriptions.append(f"- {sfx.filename} (name: '{sfx.stem}', duration: {sfx.duration:.1f}s)")
    sfx_catalog = "\n".join(sfx_descriptions)

    # Subtitle cues with timestamps
    cue_lines = []
    for c in cues[:max_cues_to_send]:
        cue_lines.append(f"[{c.index}] ({c.start:.2f}s - {c.end:.2f}s): {c.text}")
    cues_text = "\n".join(cue_lines)

    system_prompt = (
        "You are an expert audio designer for YouTube documentaries and high-retention video essays.\n"
        "Your goal is to select subtle, impactful sound effects from an available sound effects library "
        "and position them at the exact spoken moment in the video.\n\n"
        "Rules:\n"
        "1. Do NOT spam sound effects. Select only 3 to 8 of the most contextually relevant moments in total.\n"
        "2. Ensure sound effects are separated by at least 3-4 seconds.\n"
        "3. Only choose filenames from the provided Available Sound Effects list.\n"
        "4. Output strictly valid JSON matching this schema:\n"
        "[\n"
        "  {\n"
        "    \"sfx\": \"<filename>\",\n"
        "    \"cue_index\": <number>,\n"
        "    \"timestamp\": <float seconds>,\n"
        "    \"phrase\": \"<spoken phrase triggering the sound>\",\n"
        "    \"reason\": \"<short justification>\"\n"
        "  }\n"
        "]\n"
        "Do not include any conversational filler or explanation outside the JSON."
    )

    user_prompt = (
        f"Available Sound Effects in Library:\n{sfx_catalog}\n\n"
        f"Voiceover Subtitle Timeline:\n{cues_text}\n\n"
        "Select the best matching sound effects and return the JSON array:"
    )

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        temp = float(config.get("temperature", "0.0"))
    except ValueError:
        temp = 0.0
    try:
        timeout = int(config.get("timeout", "30"))
    except ValueError:
        timeout = 30

    print(f"[sfx-adder] Querying LLM ({config['model_name']}) for semantic sound effects...", file=sys.stderr)
    resp = call_openai_chat(
        base_url=config["base_url"],
        api_key=config["api_key"],
        model_name=config["model_name"],
        messages=messages,
        temperature=temp,
        timeout=timeout,
    )

    if not resp:
        print("[sfx-adder] Warning: Empty or failed response from LLM.", file=sys.stderr)
        return []

    parsed = extract_json_block(resp)
    if isinstance(parsed, list):
        return parsed

    print("[sfx-adder] Warning: LLM did not return a valid JSON array.", file=sys.stderr)
    return []


def fallback_keyword_alignment(
    cues: List[SubtitleCue],
    available_sfx: List[SoundEffectFile],
    min_interval: float = 3.0,
) -> List[Dict[str, Any]]:
    """Deterministic fallback that aligns SFX based on sound filenames and keyword matching."""
    matches: List[Dict[str, Any]] = []
    last_assigned_time = -999.0

    # Build keyword lookup per sfx file
    sfx_lookup: List[Tuple[SoundEffectFile, Set[str]]] = []
    for sfx in available_sfx:
        clean_stem = sfx.stem.lower().replace("_", "-")
        keywords = set(re.findall(r"[a-z0-9]+", clean_stem))
        for key, extra_words in FALLBACK_KEYWORD_MAP.items():
            if key in clean_stem or clean_stem in key:
                keywords.update(extra_words)
        sfx_lookup.append((sfx, keywords))

    for cue in cues:
        cue_words = [w.lower() for w in re.findall(r"[a-z0-9]+", cue.text)]
        if not cue_words:
            continue

        for sfx, keywords in sfx_lookup:
            matching_words = [w for w in cue_words if w in keywords]
            if matching_words:
                # Find the position of the first matching word
                first_match = matching_words[0]
                idx = cue_words.index(first_match)
                # Proportional offset within cue
                offset_ratio = idx / max(1, len(cue_words))
                ts = round(cue.start + offset_ratio * (cue.end - cue.start), 2)

                if ts - last_assigned_time >= min_interval:
                    matches.append({
                        "sfx": sfx.filename,
                        "cue_index": cue.index,
                        "timestamp": ts,
                        "phrase": first_match,
                        "reason": f"Keyword match: '{first_match}' in narration",
                    })
                    last_assigned_time = ts
                    break  # Avoid stacking multiple sounds on the same cue

    return matches


# --------------------------------------------------------------------------- #
# Event Scheduling & Timeline Construction
# --------------------------------------------------------------------------- #

def build_sfx_schedule(
    beats: List[Dict[str, Any]],
    cues: List[SubtitleCue],
    voiceover_text: str,
    library: Dict[str, SoundEffectFile],
    config: Dict[str, str],
    video_duration: float,
    pop_volume: float = 0.4,
    sfx_volume: float = 0.5,
    include_first_beat: bool = False,
    min_interval: float = 3.0,
) -> List[SFXEvent]:
    """Compile the master chronological schedule of beat pops and semantic SFX."""
    events: List[SFXEvent] = []

    # 1. Beat transitions: soft-pop.mp3 on every beat
    # Find soft-pop in library
    pop_sfx = None
    for name, sfx in library.items():
        if "soft-pop" in name.lower() or "pop" in name.lower():
            pop_sfx = sfx
            break

    if pop_sfx:
        for beat in beats:
            start_t = float(beat.get("start_time", 0.0))
            image_id = beat.get("image_id", "?")
            # If not including first beat, skip timestamps very close to 0.0s
            if not include_first_beat and start_t < 0.05:
                continue
            if start_t >= video_duration:
                continue

            events.append(SFXEvent(
                time=round(start_t, 3),
                sfx_file=pop_sfx.filename,
                sfx_path=str(pop_sfx.path),
                type="beat",
                volume=pop_volume,
                reason=f"Beat transition (Image #{image_id})",
            ))
    else:
        print("[sfx-adder] Note: soft-pop.mp3 not found in library; skipping beat pops.", file=sys.stderr)

    # 2. Semantic SFX: all other audio files in library
    semantic_candidates = [sfx for name, sfx in library.items() if sfx != pop_sfx]

    if semantic_candidates and cues:
        # Query LLM
        semantic_matches = query_llm_for_sfx(cues, voiceover_text, semantic_candidates, config)

        # If LLM didn't return matches, fallback to keyword matching
        if not semantic_matches:
            print("[sfx-adder] Falling back to keyword-based sound design.", file=sys.stderr)
            semantic_matches = fallback_keyword_alignment(cues, semantic_candidates, min_interval=min_interval)

        # Validate and schedule semantic matches
        last_semantic_t = -999.0
        for item in semantic_matches:
            sfx_name = item.get("sfx", "").strip()
            # Match against available candidates
            matched_sfx = library.get(sfx_name)
            if not matched_sfx:
                # Try matching by stem
                for c in semantic_candidates:
                    if c.stem.lower() == sfx_name.lower() or c.filename.lower() == sfx_name.lower():
                        matched_sfx = c
                        break

            if not matched_sfx:
                continue

            ts = float(item.get("timestamp", -1.0))
            if ts < 0.0:
                # Lookup cue
                cue_idx = item.get("cue_index")
                for c in cues:
                    if c.index == cue_idx:
                        ts = c.start
                        break

            if ts < 0.0 or ts >= video_duration:
                continue

            # Ensure separation from previous semantic SFX
            if ts - last_semantic_t < min_interval:
                continue

            events.append(SFXEvent(
                time=round(ts, 3),
                sfx_file=matched_sfx.filename,
                sfx_path=str(matched_sfx.path),
                type="semantic",
                volume=sfx_volume,
                reason=str(item.get("reason", "Contextual SFX")),
                cue_index=item.get("cue_index"),
                phrase=item.get("phrase"),
            ))
            last_semantic_t = ts

    # Sort all events chronologically
    events.sort(key=lambda e: e.time)
    return events


# --------------------------------------------------------------------------- #
# FFmpeg Audio Mixer
# --------------------------------------------------------------------------- #

def render_sfx_mix(
    video_path: Path,
    events: List[SFXEvent],
    output_path: Path,
    dry_run: bool = False,
) -> None:
    """Mix sound effects into the video's audio using FFmpeg adelay + amix with video stream copy."""
    if not events:
        print("[sfx-adder] No sound effects scheduled. Copying video directly.", file=sys.stderr)
        if not dry_run:
            shutil.copy2(video_path, output_path)
        return

    # Group events by audio file path so we can assign distinct inputs
    unique_files: List[str] = sorted(list({e.sfx_path for e in events}))
    file_to_input_idx = {f: idx + 1 for idx, f in enumerate(unique_files)}

    # Count how many times each file is used to split streams
    file_usage_counts: Dict[str, int] = {f: 0 for f in unique_files}
    for e in events:
        file_usage_counts[e.sfx_path] += 1

    filter_parts: List[str] = []
    file_split_tags: Dict[str, List[str]] = {f: [] for f in unique_files}

    # Step 1: asplit each input audio stream into the number of times it will be delayed
    for f in unique_files:
        input_idx = file_to_input_idx[f]
        count = file_usage_counts[f]
        tags = [f"s_{input_idx}_{i}" for i in range(count)]
        file_split_tags[f] = tags
        tag_str = "".join(f"[{t}]" for t in tags)
        if count > 1:
            filter_parts.append(f"[{input_idx}:a]asplit={count}{tag_str}")
        else:
            filter_parts.append(f"[{input_idx}:a]anull[{tags[0]}]")

    # Step 2: adelay + volume for each event
    file_used_pointer: Dict[str, int] = {f: 0 for f in unique_files}
    amix_inputs: List[str] = ["[0:a]"]

    for idx, event in enumerate(events):
        ptr = file_used_pointer[event.sfx_path]
        stream_tag = file_split_tags[event.sfx_path][ptr]
        file_used_pointer[event.sfx_path] += 1

        delay_ms = max(0, int(round(event.time * 1000)))
        out_tag = f"d_{idx}"
        # adelay delays each audio channel (delays=X|X)
        filter_parts.append(f"[{stream_tag}]adelay={delay_ms}|{delay_ms},volume={event.volume:.2f}[{out_tag}]")
        amix_inputs.append(f"[{out_tag}]")

    # Step 3: amix all streams together without scaling down the primary narration (normalize=0)
    total_mix_inputs = len(amix_inputs)
    inputs_str = "".join(amix_inputs)
    filter_parts.append(
        f"{inputs_str}amix=inputs={total_mix_inputs}:duration=first:dropout_transition=0:normalize=0[aout]"
    )

    filtergraph = ";\n".join(filter_parts)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    # Input 0: base video
    cmd += ["-i", str(video_path)]
    # Inputs 1..K: unique SFX files
    for f in unique_files:
        cmd += ["-i", f]

    cmd += [
        "-filter_complex", filtergraph,
        "-map", "0:v:0",
        "-c:v", "copy",
        "-map", "[aout]",
        "-c:a", "aac",
        "-b:a", "192k",
    ]

    temp_output = output_path.with_suffix(".tmp.mp4")
    cmd.append(str(temp_output))

    if dry_run:
        print("[sfx-adder] Dry-run enabled. Generated FFmpeg command:\n")
        print(" ".join(cmd))
        return

    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[sfx-adder] Rendering video with {len(events)} sound effects mixed in...", file=sys.stderr)
    start_time = time.time()

    try:
        subprocess.run(cmd, check=True)
        os.replace(temp_output, output_path)
        elapsed = time.time() - start_time
        print(f"[sfx-adder] Success! Output saved to: {output_path} (took {elapsed:.1f}s)", file=sys.stderr)
    except subprocess.CalledProcessError as exc:
        if temp_output.exists():
            temp_output.unlink(missing_ok=True)
        print(f"[sfx-adder] Error: FFmpeg audio mixing failed with exit code {exc.returncode}.", file=sys.stderr)
        sys.exit(exc.returncode)


# --------------------------------------------------------------------------- #
# CLI Entry Point
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add beat pop transitions and LLM-decided semantic sound effects to video.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--video", required=True, help="Input video path (e.g. final.mp4)")
    parser.add_argument("--srt", required=True, help="Subtitle SRT file path (<slug>.srt)")
    parser.add_argument("--beat", required=False, help="Beat timing file path (beat.json or beat.md)")
    parser.add_argument("--voiceover", required=False, help="Voiceover transcript file (voiceover.md)")
    parser.add_argument("--library", required=False, help="Directory containing SFX audio files (default: sfx-adder/library)")
    parser.add_argument("--out", required=False, help="Output video path (e.g. final_sfx.mp4)")
    parser.add_argument("--cues-out", required=False, help="Path to save scheduled cues JSON (sfx_cues.json)")
    parser.add_argument("--sfx-volume", type=float, default=0.5, help="Volume multiplier for contextual sound effects (0.0 - 1.0)")
    parser.add_argument("--pop-volume", type=float, default=0.4, help="Volume multiplier for beat transition pops (0.0 - 1.0)")
    parser.add_argument("--include-first-beat", action="store_true", help="Play beat pop at start time 0.0s (default is off)")
    parser.add_argument("--min-interval", type=float, default=3.0, help="Minimum interval in seconds between semantic SFX")
    parser.add_argument("--dry-run", action="store_true", help="Print timeline and FFmpeg command without rendering")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging")

    args = parser.parse_args()

    check_tool("ffmpeg")
    check_tool("ffprobe")

    video_path = Path(args.video).resolve()
    if not video_path.is_file():
        print(f"Error: Input video not found: {video_path}", file=sys.stderr)
        sys.exit(66)

    srt_path = Path(args.srt).resolve()
    if not srt_path.is_file():
        print(f"Error: Subtitle SRT file not found: {srt_path}", file=sys.stderr)
        sys.exit(66)

    script_dir = Path(__file__).resolve().parent
    library_dir = Path(args.library).resolve() if args.library else script_dir / "library"
    if not library_dir.is_dir():
        print(f"Error: Sound effects library folder not found: {library_dir}", file=sys.stderr)
        sys.exit(66)

    output_path = Path(args.out).resolve() if args.out else video_path.with_name("final_sfx.mp4")

    # Load media properties
    video_dur = probe_duration(video_path) or 60.0
    has_audio = probe_has_audio(video_path)
    if not has_audio:
        print(f"Warning: Input video {video_path} does not appear to contain an audio stream.", file=sys.stderr)

    # Load beats
    beats = []
    if args.beat:
        beat_path = Path(args.beat).resolve()
        beats = load_beats(beat_path)
    else:
        # Check sibling beat.json
        candidate_beat = video_path.parent / "beat.json"
        if candidate_beat.is_file():
            beats = load_beats(candidate_beat)

    # Load voiceover text
    voiceover_text = ""
    if args.voiceover:
        vo_path = Path(args.voiceover).resolve()
        if vo_path.is_file():
            voiceover_text = vo_path.read_text(encoding="utf-8", errors="replace")

    # Parse SRT cues
    cues = parse_srt(srt_path)
    if not voiceover_text and cues:
        voiceover_text = " ".join(c.text for c in cues)

    # Scan library
    library_files = scan_library(library_dir)
    if not library_files:
        print(f"Error: No audio files found in library directory: {library_dir}", file=sys.stderr)
        sys.exit(1)

    # Load environment config
    config = get_env_config(script_dir)

    print(f"[sfx-adder] Found {len(library_files)} audio files in library.", file=sys.stderr)
    print(f"[sfx-adder] Video duration: {video_dur:.2f}s | Beats: {len(beats)} | Subtitle Cues: {len(cues)}", file=sys.stderr)

    # Build schedule
    events = build_sfx_schedule(
        beats=beats,
        cues=cues,
        voiceover_text=voiceover_text,
        library=library_files,
        config=config,
        video_duration=video_dur,
        pop_volume=args.pop_volume,
        sfx_volume=args.sfx_volume,
        include_first_beat=args.include_first_beat,
        min_interval=args.min_interval,
    )

    beat_count = sum(1 for e in events if e.type == "beat")
    semantic_count = sum(1 for e in events if e.type == "semantic")
    print(f"[sfx-adder] Scheduled {len(events)} total SFX events ({beat_count} beat pops, {semantic_count} contextual SFX).", file=sys.stderr)

    # Print summary of scheduled events
    if args.verbose or args.dry_run:
        print("\n--- Scheduled SFX Timeline ---")
        for e in events:
            cue_info = f" [Cue #{e.cue_index}]" if e.cue_index is not None else ""
            print(f"  [{e.time:6.2f}s] {e.type.upper():8s} | {e.sfx_file:20s} (vol={e.volume:.2f}) | {e.reason}{cue_info}")
        print("------------------------------\n")

    # Export sfx_cues.json
    cues_out_path = Path(args.cues_out).resolve() if args.cues_out else output_path.parent / "sfx_cues.json"
    cues_data = [asdict(e) for e in events]
    cues_out_path.parent.mkdir(parents=True, exist_ok=True)
    temp_cues = cues_out_path.with_suffix(".tmp.json")
    with open(temp_cues, "w", encoding="utf-8") as f:
        json.dump(cues_data, f, indent=2)
    os.replace(temp_cues, cues_out_path)
    print(f"[sfx-adder] Wrote timeline schedule to: {cues_out_path}", file=sys.stderr)

    # Render audio mix
    render_sfx_mix(
        video_path=video_path,
        events=events,
        output_path=output_path,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
