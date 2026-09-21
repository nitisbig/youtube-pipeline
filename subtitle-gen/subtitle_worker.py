#!/usr/bin/env python3
"""
subtitle_worker.py - Burn styled and animated subtitles onto video using FFmpeg and ASS.

Reads an existing subtitle SRT file, parses and formats the cues according to
chosen style and animation presets (Hormozi, Karaoke, Modern, Neon, Cinematic, etc.),
generates an Advanced SubStation Alpha (.ass) subtitle file, and burns it into
the video using FFmpeg's native libass filter.

Examples:
    python3 subtitle_worker.py --video out/demo/final.mp4 --srt out/demo/demo.srt --out out/demo/final_subtitled.mp4
    python3 subtitle_worker.py --video ... --srt ... --out ... --style hormozi --animation pop --max-words 3
    python3 subtitle_worker.py --video ... --srt ... --out ... --style karaoke --animation bounce
    python3 subtitle_worker.py --list-styles
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
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

editor_dir = Path(__file__).resolve().parent.parent / "editor"
if str(editor_dir) not in sys.path:
    sys.path.insert(0, str(editor_dir))
try:
    from ffmpeg_gpu import get_video_encoder_config, str2bool, VideoEncoderConfig
except ImportError:
    def str2bool(val):
        return str(val).strip().lower() in ("yes", "true", "t", "y", "1")

    def get_video_encoder_config(use_gpu=False, preset="fast", crf=18):
        from dataclasses import dataclass
        @dataclass
        class VideoEncoderConfig:
            encoder_type: str = "cpu"
            codec: str = "libx264"
            args: list = None
            needs_hwupload: bool = False
            vaapi_device: str = None
        return VideoEncoderConfig(
            encoder_type="cpu", codec="libx264",
            args=["-c:v", "libx264", "-preset", preset, "-crf", str(crf), "-pix_fmt", "yuv420p"],
            needs_hwupload=False, vaapi_device=None,
        )

# --------------------------------------------------------------------------- #
# Constants & Presets
# --------------------------------------------------------------------------- #

STYLES = [
    "hormozi",
    "classic",
    "yellow_classic",
    "modern",
    "karaoke",
    "neon",
    "cinematic",
    "boxed",
    "comic",
]

ANIMATIONS = [
    "pop",
    "bounce",
    "fade",
    "slide_up",
    "slide_down",
    "typewriter",
    "glow_pulse",
    "none",
]

POSITIONS = ["bottom", "middle", "top"]

STYLE_HELP = {
    "hormozi": "Bold uppercase yellow text (#FFDE00), thick black outline, punchy drop shadow (viral Shorts/Reels style).",
    "classic": "Clean white text (#FFFFFF), subtle black outline, soft translucent drop shadow (standard YouTube style).",
    "yellow_classic": "Classic yellow text (#FFE800) in clean sans-serif (Liberation Sans/Arial), subtle black outline, soft translucent drop shadow (iconic cinema / retro film / anime subtitle aesthetic).",
    "modern": "Sleek sans-serif white text over a semi-transparent dark rounded/pill background box.",
    "karaoke": "Word-by-word active highlight (current word pops in vibrant cyan/gold while phrase stays readable).",
    "neon": "Cyberpunk vibrant cyan text with glowing neon colored halo/blur and bright outline.",
    "cinematic": "Elegant serif/clean font, pale gold/white text, wide letter spacing, lower-third placement.",
    "boxed": "High-contrast bold white text inside a solid dark rectangle badge (news/editorial style).",
    "comic": "Playful bold font, bright yellow fill, extra-thick cartoon black outline and offset shadow.",
}

ANIMATION_HELP = {
    "pop": "Fast zoom-in punch when cue appears (starts at 80% scale and quickly snaps to 100%).",
    "bounce": "Energetic spring bounce (70% -> 118% -> 100%) on cue appearance.",
    "fade": "Smooth alpha fade in and fade out (120ms).",
    "slide_up": "Smoothly slides up into place from slightly below with quick fade.",
    "slide_down": "Smoothly slides down into place from slightly above with quick fade.",
    "typewriter": "Sequential progressive reveal of words.",
    "glow_pulse": "Glow/blur pulses brightly when cue appears, then settles.",
    "none": "Static display without motion.",
}


# --------------------------------------------------------------------------- #
# Helpers & Formatting
# --------------------------------------------------------------------------- #


def check_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"Error: {name} is not installed or not in PATH. Please install ffmpeg.")


def hex_to_ass_color(hex_color: str, alpha: int = 0) -> str:
    """Convert #RRGGBB hex color to ASS format &HAABBGGRR (Alpha, Blue, Green, Red)."""
    hex_color = hex_color.strip().lstrip("#")
    if len(hex_color) == 6:
        r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    elif len(hex_color) == 3:
        r, g, b = hex_color[0] * 2, hex_color[1] * 2, hex_color[2] * 2
    else:
        r, g, b = "FF", "FF", "FF"
    alpha_clamped = max(0, min(255, alpha))
    return f"&H{alpha_clamped:02X}{b.upper()}{g.upper()}{r.upper()}&"


def format_ass_time(seconds: float) -> str:
    """Convert float seconds into ASS timestamp H:MM:SS.cc (centiseconds)."""
    seconds = max(0.0, seconds)
    hrs = int(seconds // 3600)
    rem = seconds % 3600
    mins = int(rem // 60)
    secs = rem % 60
    centis = int(round((secs - int(secs)) * 100))
    int_secs = int(secs)
    if centis >= 100:
        int_secs += 1
        centis -= 100
        if int_secs >= 60:
            mins += 1
            int_secs -= 60
            if mins >= 60:
                hrs += 1
                mins -= 60
    return f"{hrs}:{mins:02d}:{int_secs:02d}.{centis:02d}"


def parse_srt_time(time_str: str) -> float:
    """Parse SRT timestamp '00:01:23,456' into seconds."""
    time_str = time_str.strip().replace(".", ",")
    parts = time_str.split(":")
    if len(parts) == 3:
        h = float(parts[0])
        m = float(parts[1])
        s_parts = parts[2].split(",")
        s = float(s_parts[0])
        ms = float(s_parts[1]) if len(s_parts) > 1 else 0.0
        return h * 3600 + m * 60 + s + (ms / 1000.0)
    return 0.0


def contains_devanagari(text: str) -> bool:
    """Check if string contains Devanagari characters (Nepali, Hindi, etc.)."""
    for ch in text:
        if "\u0900" <= ch <= "\u097F":
            return True
    return False


@dataclass
class SrtCue:
    start: float
    end: float
    text: str


def parse_srt(srt_content: str) -> List[SrtCue]:
    """Parse an SRT string into a list of SrtCue objects."""
    cues: List[SrtCue] = []
    # Normalize line endings
    content = srt_content.replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", content.strip())

    time_pattern = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3})"
    )

    for block in blocks:
        lines = [line.strip() for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        time_match = None
        text_start_idx = 0
        for idx, line in enumerate(lines):
            match = time_pattern.search(line)
            if match:
                time_match = match
                text_start_idx = idx + 1
                break

        if time_match and text_start_idx < len(lines):
            start = parse_srt_time(time_match.group(1))
            end = parse_srt_time(time_match.group(2))
            text = " ".join(lines[text_start_idx:])
            # Strip simple HTML formatting tags if present in SRT
            text = re.sub(r"<[^>]+>", "", text).strip()
            if text and end > start:
                cues.append(SrtCue(start=start, end=end, text=text))

    return cues


def probe_video_metadata(video_path: Path) -> Tuple[int, int, float]:
    """Return (width, height, duration) of input video via ffprobe."""
    check_tool("ffprobe")
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,duration",
                "-of",
                "json",
                str(video_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        width = int(stream.get("width", 1920))
        height = int(stream.get("height", 1080))
        duration = float(stream.get("duration", 0.0))
        return width, height, duration
    except Exception:
        # Fallback to standard 1080p
        return 1920, 1080, 0.0


# --------------------------------------------------------------------------- #
# Subtitle Chunking & ASS Generation
# --------------------------------------------------------------------------- #


@dataclass
class SubtitleChunk:
    start: float
    end: float
    words: List[str]
    full_text: str


def chunk_cues(cues: List[SrtCue], max_words: int = 3, uppercase: bool = False) -> List[SubtitleChunk]:
    """
    Split SRT cues into smaller word chunks for fast-paced, modern caption display.
    If max_words <= 0, cues are kept as original lines.
    Time is proportionally distributed across word chunks based on character count.
    """
    chunks: List[SubtitleChunk] = []

    for cue in cues:
        raw_text = cue.text.strip()
        if uppercase:
            raw_text = raw_text.upper()

        words = raw_text.split()
        if not words:
            continue

        if max_words <= 0 or len(words) <= max_words:
            chunks.append(
                SubtitleChunk(
                    start=cue.start,
                    end=cue.end,
                    words=words,
                    full_text=raw_text,
                )
            )
            continue

        # Split words into groups of max_words
        word_groups: List[List[str]] = []
        for i in range(0, len(words), max_words):
            word_groups.append(words[i : i + max_words])

        # Compute total characters in the cue to distribute time proportionally
        total_chars = sum(len(w) for w in words)
        total_duration = max(0.1, cue.end - cue.start)
        cur_start = cue.start

        for idx, group in enumerate(word_groups):
            group_chars = sum(len(w) for w in group)
            ratio = group_chars / total_chars if total_chars > 0 else 1.0 / len(word_groups)
            group_duration = total_duration * ratio

            # Ensure minimal duration per chunk so it remains legible
            group_duration = max(0.25, group_duration)
            cur_end = cur_start + group_duration
            if idx == len(word_groups) - 1:
                cur_end = max(cur_end, cue.end)

            chunks.append(
                SubtitleChunk(
                    start=cur_start,
                    end=cur_end,
                    words=group,
                    full_text=" ".join(group),
                )
            )
            cur_start = cur_end

    return chunks


def build_ass_content(
    chunks: List[SubtitleChunk],
    video_width: int,
    video_height: int,
    style: str = "hormozi",
    animation: str = "pop",
    position: str = "bottom",
    font_name: str = "",
    font_size: int = 0,
    primary_color_override: str = "",
    highlight_color_override: str = "",
    outline_color_override: str = "",
    outline_width_override: int = -1,
    margin_v_override: int = -1,
    italic: bool = False,
) -> str:
    """Generate complete ASS subtitle file content based on style and animation presets."""

    is_vertical = video_height > video_width
    scale_factor = video_height / 1080.0

    # If font_name is given as a yellow_classic style alias, route to yellow_classic style
    if font_name and font_name.strip().lower() in (
        "yellow_classic",
        "yellow classic",
        "classic_yellow",
        "classic yellow",
        "yello_classic",
        "yello classic",
    ):
        style = "yellow_classic"
        font_name = ""

    # Auto-detect Devanagari to choose correct font if not manually specified
    has_devanagari = any(contains_devanagari(chunk.full_text) for chunk in chunks)

    # 1. Base typography & positioning
    if not font_name:
        if has_devanagari:
            font_name = "Noto Sans Devanagari"
        else:
            font_name = "DejaVu Sans"

    # Alignment (ASS keypad format): 2 = bottom-center, 5 = middle-center, 8 = top-center
    pos_map = {"bottom": 2, "middle": 5, "top": 8}
    alignment = pos_map.get(position, 2)

    # Default vertical margin: for 9:16 vertical video (Shorts/TikTok), bottom UI covers ~25%
    if margin_v_override >= 0:
        margin_v = margin_v_override
    elif alignment == 2:  # bottom
        margin_v = int(280 * scale_factor) if is_vertical else int(70 * scale_factor)
    elif alignment == 8:  # top
        margin_v = int(120 * scale_factor) if is_vertical else int(70 * scale_factor)
    else:  # middle
        margin_v = 0

    margin_lr = int(40 * scale_factor)

    # 2. Style configuration
    # Default base values for 1080p
    base_font_size = 52
    bold = -1  # ASS -1 is true, 0 is false
    italic_code = -1 if italic else 0
    border_style = 1  # 1 = outline + shadow, 3 = opaque box
    outline = 4
    shadow = 2
    spacing = 0

    primary_hex = "#FFFFFF"
    secondary_hex = "#FFFFFF"
    outline_hex = "#000000"
    back_hex = "#000000"
    back_alpha = 128  # 50% transparent for shadow

    highlight_hex = "#FFDE00"  # bright yellow default

    if style == "hormozi":
        base_font_size = 58
        primary_hex = "#FFDE00"  # Vibrant Yellow
        outline_hex = "#000000"
        back_hex = "#000000"
        outline = 5
        shadow = 3
        back_alpha = 80
        highlight_hex = "#00FF44"  # Neon green highlight
    elif style == "classic":
        base_font_size = 46
        primary_hex = "#FFFFFF"
        outline_hex = "#000000"
        outline = 2.5
        shadow = 2
        back_alpha = 150
        highlight_hex = "#FFFF00"
    elif style in ("yellow_classic", "classic_yellow", "yello_classic"):
        base_font_size = 46
        if not font_name or font_name == "DejaVu Sans":
            font_name = "Liberation Sans"
        primary_hex = "#FFE800"  # Iconic cinema / anime yellow
        outline_hex = "#000000"
        outline = 2.5
        shadow = 2
        back_alpha = 150
        highlight_hex = "#FFFFFF"
    elif style == "modern":
        base_font_size = 48
        primary_hex = "#FFFFFF"
        outline_hex = "#111111"
        back_hex = "#111111"
        border_style = 3  # Pill / box background
        outline = 6
        shadow = 0
        back_alpha = 100  # semi-transparent box
        highlight_hex = "#00F0FF"
    elif style == "karaoke":
        base_font_size = 54
        primary_hex = "#E0E0E0"  # subtle off-white base
        outline_hex = "#000000"
        outline = 4
        shadow = 2
        back_alpha = 100
        highlight_hex = "#00F0FF"  # vibrant cyan highlight
    elif style == "neon":
        base_font_size = 52
        primary_hex = "#00FFFF"  # Neon Cyan
        outline_hex = "#003366"  # Deep blue outline
        outline = 4
        shadow = 0
        back_alpha = 0
        highlight_hex = "#FF00FF"  # Magenta
    elif style == "cinematic":
        base_font_size = 42
        bold = 0
        if not font_name or font_name == "DejaVu Sans":
            font_name = "DejaVu Serif"
        primary_hex = "#F5F5F0"
        outline_hex = "#1A1A1A"
        outline = 2
        shadow = 2
        spacing = 2
        highlight_hex = "#E5C07B"
    elif style == "boxed":
        base_font_size = 50
        primary_hex = "#FFFFFF"
        outline_hex = "#000000"
        back_hex = "#000000"
        border_style = 3  # Opaque badge
        outline = 6
        shadow = 0
        back_alpha = 30  # High opacity
        highlight_hex = "#FFCC00"
    elif style == "comic":
        base_font_size = 56
        primary_hex = "#FFE600"
        outline_hex = "#111111"
        outline = 6
        shadow = 4
        back_alpha = 40
        highlight_hex = "#FF3366"

    # Apply overrides
    if primary_color_override:
        primary_hex = primary_color_override
    if highlight_color_override:
        highlight_hex = highlight_color_override
    if outline_color_override:
        outline_hex = outline_color_override
    if outline_width_override >= 0:
        outline = outline_width_override

    calculated_font_size = int(round((font_size if font_size > 0 else base_font_size) * scale_factor))
    calculated_outline = max(1.0, round(outline * scale_factor, 1))
    calculated_shadow = max(0.0, round(shadow * scale_factor, 1))

    primary_ass = hex_to_ass_color(primary_hex, 0)
    secondary_ass = hex_to_ass_color(secondary_hex, 0)
    outline_ass = hex_to_ass_color(outline_hex, 0)
    back_ass = hex_to_ass_color(back_hex, back_alpha)
    highlight_ass = hex_to_ass_color(highlight_hex, 0)

    # 3. Build ASS header and styles
    ass_lines = [
        "[Script Info]",
        "Title: YouTube Pipeline Generated Subtitles",
        "ScriptType: v4.00+",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.601",
        f"PlayResX: {video_width}",
        f"PlayResY: {video_height}",
        "",
        "[V4+ Styles]",
        (
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
            "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding"
        ),
        (
            f"Style: Default,{font_name},{calculated_font_size},{primary_ass},{secondary_ass},{outline_ass},"
            f"{back_ass},{bold},{italic_code},0,0,100,100,{spacing},0,{border_style},{calculated_outline},"
            f"{calculated_shadow},{alignment},{margin_lr},{margin_lr},{margin_v},1"
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    # 4. Generate dialogue events with styles and animations
    for chunk in chunks:
        start_str = format_ass_time(chunk.start)
        end_str = format_ass_time(chunk.end)
        duration_ms = int(max(100, (chunk.end - chunk.start) * 1000))

        # Build animation override tags
        anim_prefix = ""
        if animation == "fade":
            fade_ms = min(150, duration_ms // 4)
            anim_prefix = f"{{\\fad({fade_ms},{fade_ms})}}"
        elif animation == "pop":
            t_ms = min(110, duration_ms // 3)
            anim_prefix = f"{{\\fscx82\\fscy82\\t(0,{t_ms},\\fscx100\\fscy100)}}"
        elif animation == "bounce":
            t1 = min(90, duration_ms // 4)
            t2 = min(170, duration_ms // 2)
            anim_prefix = f"{{\\fscx70\\fscy70\\t(0,{t1},\\fscx118\\fscy118)\\t({t1},{t2},\\fscx100\\fscy100)}}"
        elif animation == "slide_up":
            # Animate from 30px below to original position
            anim_prefix = f"{{\\fscy85\\t(0,120,\\fscy100)\\fad(90,0)}}"
        elif animation == "slide_down":
            anim_prefix = f"{{\\fscy85\\t(0,120,\\fscy100)\\fad(90,0)}}"
        elif animation == "glow_pulse":
            anim_prefix = f"{{\\blur2\\t(0,100,\\blur8)\\t(100,220,\\blur2)}}"
        elif animation == "none":
            anim_prefix = ""

        # Style-specific text rendering
        if style == "neon":
            # Add subtle glow blur tag
            anim_prefix = f"{{\\blur3}}{anim_prefix}"

        if style == "karaoke" and len(chunk.words) > 1:
            # Word-by-word active highlight within the chunk duration
            total_chars = sum(len(w) for w in chunk.words)
            w_start = chunk.start
            for w_idx, active_word in enumerate(chunk.words):
                w_ratio = len(active_word) / total_chars if total_chars > 0 else 1.0 / len(chunk.words)
                w_dur = (chunk.end - chunk.start) * w_ratio
                w_end = w_start + w_dur
                if w_idx == len(chunk.words) - 1:
                    w_end = chunk.end

                # Format current line where active_word has highlight color and scale
                word_parts = []
                for i, w in enumerate(chunk.words):
                    if i == w_idx:
                        word_parts.append(f"{{\\c{highlight_ass}\\fscx108\\fscy108}}{w}{{\\c{primary_ass}\\fscx100\\fscy100}}")
                    else:
                        word_parts.append(w)

                dialogue_text = f"{anim_prefix}{' '.join(word_parts)}"
                ass_lines.append(
                    f"Dialogue: 0,{format_ass_time(w_start)},{format_ass_time(w_end)},Default,,0,0,0,,{dialogue_text}"
                )
                w_start = w_end
        else:
            dialogue_text = f"{anim_prefix}{chunk.full_text}"
            ass_lines.append(
                f"Dialogue: 0,{start_str},{end_str},Default,,0,0,0,,{dialogue_text}"
            )

    return "\n".join(ass_lines) + "\n"


# --------------------------------------------------------------------------- #
# Subtitle Burner Engine
# --------------------------------------------------------------------------- #


def burn_subtitles(
    video_path: Path,
    srt_path: Path,
    output_path: Path,
    style: str = "hormozi",
    animation: str = "pop",
    position: str = "bottom",
    max_words: int = 3,
    uppercase: bool = False,
    italic: bool = False,
    font_name: str = "",
    font_size: int = 0,
    dry_run: bool = False,
    crf: int = 18,
    preset: str = "fast",
    use_gpu: bool = False,
) -> None:
    video = Path(video_path).resolve()
    srt = Path(srt_path).resolve()
    out = Path(output_path).resolve()

    if not video.exists():
        sys.exit(f"Error: Input video not found: {video}")
    if not srt.exists():
        sys.exit(f"Error: Subtitle SRT file not found: {srt}")

    check_tool("ffmpeg")
    check_tool("ffprobe")

    # Normalize style aliases
    if style in ("classic_yellow", "yello_classic", "yellow classic", "classic yellow"):
        style = "yellow_classic"

    print(f"Reading subtitles: {srt}")
    try:
        srt_text = srt.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        srt_text = srt.read_text(encoding="latin-1")

    cues = parse_srt(srt_text)
    if not cues:
        sys.exit(f"Error: No valid subtitle cues found in {srt}")

    print(f"Parsed {len(cues)} subtitle cues.")

    # Probe video dimensions
    width, height, duration = probe_video_metadata(video)
    print(f"Video resolution : {width}x{height} (duration: {duration:.2f}s)")
    print(f"Preset style     : {style} ({STYLE_HELP.get(style, '')})")
    print(f"Animation        : {animation} ({ANIMATION_HELP.get(animation, '')})")
    print(f"Position         : {position}")
    print(f"Italic           : {italic}")
    print(f"Max words/chunk  : {max_words if max_words > 0 else 'no chunking'}")
    print(f"GPU acceleration : {use_gpu}")

    chunks = chunk_cues(cues, max_words=max_words, uppercase=uppercase)
    print(f"Generated {len(chunks)} display chunks.")

    ass_content = build_ass_content(
        chunks=chunks,
        video_width=width,
        video_height=height,
        style=style,
        animation=animation,
        position=position,
        font_name=font_name,
        font_size=font_size,
        italic=italic,
    )

    # Write ASS file next to the output video or in the project folder
    ass_path = out.parent / f"{out.stem}.ass"
    ass_path.write_text(ass_content, encoding="utf-8")
    print(f"Wrote ASS subtitle file: {ass_path}")

    # Escape path for FFmpeg filter graph (libass)
    escaped_ass = str(ass_path).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")

    enc_config = get_video_encoder_config(use_gpu=use_gpu, preset=preset, crf=crf)

    if dry_run:
        print("\n--- Dry Run: Sample ASS Subtitle (First 25 lines) ---")
        lines = ass_content.splitlines()[:25]
        print("\n".join(lines))
        print(f"\n[Dry Run] Hardware encoder: {enc_config.encoder_type.upper()} ({enc_config.codec})")
        print("[Dry Run] FFmpeg command that would be executed:")
        vf_dry = f"ass='{escaped_ass}'"
        if enc_config.needs_hwupload:
            vf_dry += ",format=nv12,hwupload"
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
        if enc_config.vaapi_device:
            cmd += ["-vaapi_device", enc_config.vaapi_device]
        cmd += ["-i", str(video), "-vf", vf_dry] + enc_config.args + ["-c:a", "copy", str(out)]
        print(" ".join(cmd))
        return

    out.parent.mkdir(parents=True, exist_ok=True)
    temp_out = out.with_name(f"{out.stem}.tmp{out.suffix}") if out == video else out

    vf_filter = f"ass='{escaped_ass}'"
    if enc_config.needs_hwupload:
        vf_filter += ",format=nv12,hwupload"

    ffmpeg_cmd = ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y"]
    if enc_config.vaapi_device:
        ffmpeg_cmd += ["-vaapi_device", enc_config.vaapi_device]
    ffmpeg_cmd += [
        "-i", str(video),
        "-vf", vf_filter,
    ]
    ffmpeg_cmd += enc_config.args
    ffmpeg_cmd += [
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(temp_out),
    ]

    cpu_config = get_video_encoder_config(use_gpu=False, preset=preset, crf=crf)
    cpu_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "warning", "-stats", "-y",
        "-i", str(video),
        "-vf", f"ass='{escaped_ass}'",
    ] + cpu_config.args + [
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(temp_out),
    ]

    print(f"\nBurning subtitles with FFmpeg ({enc_config.encoder_type.upper()})...")
    try:
        subprocess.run(ffmpeg_cmd, check=True)
    except subprocess.CalledProcessError as exc:
        if enc_config.encoder_type != "cpu":
            print(f"\n[WARNING] FFmpeg GPU ({enc_config.encoder_type}) failed with exit code {exc.returncode}. Retrying with CPU (libx264)...")
            try:
                subprocess.run(cpu_cmd, check=True)
            except subprocess.CalledProcessError as cpu_exc:
                if temp_out != out and temp_out.exists():
                    temp_out.unlink()
                sys.exit(f"\nFFmpeg CPU fallback failed with exit code {cpu_exc.returncode}")
        else:
            if temp_out != out and temp_out.exists():
                temp_out.unlink()
            sys.exit(f"\nFFmpeg failed with exit code {exc.returncode}")
    except KeyboardInterrupt:
        if temp_out != out and temp_out.exists():
            temp_out.unlink()
        sys.exit("\nCancelled by user.")

    if temp_out != out:
        temp_out.replace(out)

    print(f"\nSubtitled video successfully created: {out}")


# --------------------------------------------------------------------------- #
# CLI Parser
# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Burn styled and animated subtitles onto video from an SRT file using FFmpeg and ASS.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--video", help="Path to input video (e.g. final.mp4)")
    parser.add_argument("--srt", help="Path to input subtitle SRT file")
    parser.add_argument("--out", help="Path to output subtitled video (e.g. final_subtitled.mp4)")

    parser.add_argument(
        "--style",
        choices=STYLES + ["classic_yellow", "yello_classic"],
        default="hormozi",
        help="Subtitle style preset (default: hormozi). Use --list-styles to see all descriptions.",
    )
    parser.add_argument(
        "--animation",
        choices=ANIMATIONS,
        default="pop",
        help="Subtitle animation preset (default: pop).",
    )
    parser.add_argument(
        "--position",
        choices=POSITIONS,
        default="bottom",
        help="Screen position: bottom, middle, top (default: bottom).",
    )
    parser.add_argument(
        "--max-words",
        type=int,
        default=3,
        help="Max words per subtitle chunk (default: 3). Set to 0 to keep original SRT lines.",
    )
    parser.add_argument(
        "--uppercase",
        action="store_true",
        default=False,
        help="Force all subtitle text to uppercase.",
    )
    parser.add_argument(
        "--italic",
        action="store_true",
        default=False,
        help="Render subtitles in italic (classic cinema/anime subtitle aesthetic).",
    )
    parser.add_argument(
        "--font",
        default="",
        help="Font family name (e.g. 'DejaVu Sans', 'Liberation Sans', 'Noto Sans Devanagari').",
    )
    parser.add_argument(
        "--font-size",
        type=int,
        default=0,
        help="Font size in pixels (default: 0 = auto-calculated based on video resolution).",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=18,
        help="FFmpeg x264 CRF quality level (default: 18, pristine quality).",
    )
    parser.add_argument(
        "--preset",
        default="fast",
        help="FFmpeg x264 encode speed preset: ultrafast, fast, medium (default: fast).",
    )
    parser.add_argument(
        "--gpu",
        nargs="?",
        const=True,
        default=False,
        type=str2bool,
        help="Use GPU acceleration (NVENC/VAAPI) for burning subtitles with CPU fallback.",
    )
    parser.add_argument(
        "--list-styles",
        action="store_true",
        help="List available subtitle styles and animations with detailed descriptions.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate ASS file and print FFmpeg command without encoding the video.",
    )

    args = parser.parse_args()

    if args.list_styles:
        print("\n=== Subtitle Styles ===")
        for s in STYLES:
            desc = STYLE_HELP.get(s, "")
            print(f"  {s:<14} : {desc}")
        print("\n=== Subtitle Animations ===")
        for a in ANIMATIONS:
            desc = ANIMATION_HELP.get(a, "")
            print(f"  {a:<14} : {desc}")
        return

    if not args.video or not args.srt or not args.out:
        parser.print_help()
        sys.exit("\nError: --video, --srt, and --out are required unless using --list-styles.")

    burn_subtitles(
        video_path=Path(args.video),
        srt_path=Path(args.srt),
        output_path=Path(args.out),
        style=args.style,
        animation=args.animation,
        position=args.position,
        max_words=args.max_words,
        uppercase=args.uppercase,
        italic=args.italic,
        font_name=args.font,
        font_size=args.font_size,
        dry_run=args.dry_run,
        crf=args.crf,
        preset=args.preset,
        use_gpu=args.gpu,
    )


if __name__ == "__main__":
    main()
