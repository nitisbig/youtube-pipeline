"""All model-facing text lives here.

If you want to change the voice, the rules, or the output contract, this is
the only file you need to edit. Every prompt also carries two machine-readable
lines (``TOPIC:`` and ``SPEC:``) so offline/mock providers and log readers can
tell exactly what was requested.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..models import Beat, Outline, ScriptRequest

SYSTEM_WRITER = (
    "You are a senior YouTube scriptwriter and storyboard director. "
    "You write narration that is spoken aloud: concrete, rhythmic, no filler, "
    "no stage directions, no markdown, no emoji, no speaker labels. "
    "You always obey the exact output contract you are given and you always "
    "return valid JSON when asked for JSON."
)


def _tag_block(title: str, tags: Dict[str, str]) -> str:
    if not tags:
        return ""
    lines = "\n".join(f"- {key}: {value}" for key, value in tags.items())
    return f"{title}\n{lines}\n"


def _instructions_block(request: ScriptRequest, scope: str = "script") -> str:
    """Free-form producer notes. Empty string when none were given."""
    text = (request.instructions or "").strip()
    if not text:
        return ""
    if scope == "images":
        lead = (
            "Producer instructions (apply whatever concerns the visuals; "
            "ignore parts that are only about the narration):"
        )
    else:
        lead = (
            "Producer instructions (highest priority - follow these even where "
            "they conflict with the creative tags, but never break the JSON contract):"
        )
    return f"\n{lead}\n{text}\n"


def _spec(kind: str, count: int, start: int = 1) -> str:
    return f"SPEC: kind={kind} count={count} start={start}"


def _footer(kind: str, count: int, start: int = 1) -> str:
    return f"Return JSON only. No prose, no code fences.\n{_spec(kind, count, start)}"


# --------------------------------------------------------------------------- #
# Outline
# --------------------------------------------------------------------------- #


def outline_prompt(request: ScriptRequest, research_notes: str = "") -> str:
    total = request.beat_count
    research = f"\nResearch notes you must respect:\n{research_notes.strip()}\n" if research_notes.strip() else ""
    title_rule = (
        f'Use exactly this title: "{request.title}".'
        if request.title
        else "Write a specific, curiosity-driven title (no clickbait punctuation)."
    )
    return f"""TOPIC: {request.topic}

Plan a {total}-beat voiceover script for a faceless YouTube video.
A beat is one spoken line of roughly {request.words_per_beat} words.

{_tag_block("Creative tags:", request.narration_tags())}{_instructions_block(request)}{research}
{title_rule}
Break the video into 4-7 sections. The `beats` values must sum to exactly {total}.

JSON shape:
{{"title": "...",
  "logline": "one sentence promise of the video",
  "sections": [{{"name": "...", "summary": "what happens here", "beats": 12}}]}}

{_footer("outline", total)}"""


# --------------------------------------------------------------------------- #
# Beats
# --------------------------------------------------------------------------- #


def beats_prompt(
    request: ScriptRequest,
    outline: Outline,
    start: int,
    count: int,
    previous: Optional[List[Beat]] = None,
) -> str:
    total = request.beat_count
    end = start + count - 1

    sections = "\n".join(
        f"- {s.get('name', 'Section')} ({s.get('beats', '?')} beats): {s.get('summary', '')}"
        for s in (outline.sections or [])
    )
    outline_block = (
        f"Video title: {outline.title}\nLogline: {outline.logline}\nStructure:\n{sections}\n"
        if outline.title or sections
        else ""
    )

    continuity = ""
    if previous:
        tail = "\n".join(f"beat{b.index}: {b.narration}" for b in previous[-4:])
        continuity = (
            f"\nThe previous beats ended like this. Continue seamlessly, do not repeat them:\n{tail}\n"
        )

    research = (
        f"\nResearch notes you must respect:\n{outline.research_notes.strip()}\n"
        if outline.research_notes.strip()
        else ""
    )

    return f"""TOPIC: {request.topic}

Write beats {start} to {end} of a {total}-beat voiceover script.

{outline_block}{_tag_block("Creative tags:", request.narration_tags())}{_instructions_block(request)}{continuity}{research}
Rules:
- Exactly {count} beats, indexed {start} to {end}, in order.
- Each `narration` is ONE spoken line, about {request.words_per_beat} words (never more than double that).
- Plain spoken {request.language}. No markdown, no labels, no quotes around the line, no emoji.
- Never mention beats, scenes, cameras or the video itself inside `narration`.
- Each beat advances the story; no restating the previous beat.
- `visual_hint` is a short note (max 12 words) describing what the viewer should see.

JSON shape:
{{"beats": [{{"index": {start}, "narration": "...", "visual_hint": "..."}}]}}

{_footer("beats", count, start)}"""


# --------------------------------------------------------------------------- #
# Image prompts
# --------------------------------------------------------------------------- #


def image_prompts_prompt(request: ScriptRequest, beats: List[Beat]) -> str:
    start = beats[0].index
    count = len(beats)
    end = beats[-1].index

    listing = "\n".join(
        f"beat{b.index}: {b.narration}" + (f" | visual: {b.visual_hint}" if b.visual_hint else "")
        for b in beats
    )

    return f"""TOPIC: {request.topic}

Write ONE text-to-image prompt for each beat below ({count} prompts, indexes {start} to {end}).

Beats:
{listing}

{_tag_block("Art direction tags:", request.visual_tags())}{_instructions_block(request, scope="images")}
Rules for every prompt:
- Self-sufficient: it must make sense alone, with no memory of other prompts.
  Never write "same as before", "the previous scene", "as above", or "see beat N".
- Restate the subject, setting, action, lighting, mood and style every time.
- 30-60 words, one paragraph, comma-separated visual phrases. No line breaks.
- Describe only what is visible. No narration text, no captions, no watermarks, no letters.
- Compose for a {request.aspect_ratio} frame.
- Do NOT include the aspect ratio or any prefix; that is added automatically.

JSON shape:
{{"prompts": [{{"index": {start}, "prompt": "..."}}]}}

{_footer("image_prompts", count, start)}"""


# --------------------------------------------------------------------------- #
# Single-item repair calls
# --------------------------------------------------------------------------- #


def single_beat_prompt(request: ScriptRequest, outline: Outline, index: int) -> str:
    return f"""TOPIC: {request.topic}

Write exactly ONE missing beat (index {index}) for the script "{outline.title or request.topic}".

{_tag_block("Creative tags:", request.narration_tags())}{_instructions_block(request)}
One spoken line of about {request.words_per_beat} words, plain {request.language}, no labels.

JSON shape:
{{"beats": [{{"index": {index}, "narration": "...", "visual_hint": "..."}}]}}

{_footer("beats", 1, index)}"""


def single_image_prompt(request: ScriptRequest, beat: Beat) -> str:
    return f"""TOPIC: {request.topic}

Write exactly ONE self-sufficient text-to-image prompt for this beat.

beat{beat.index}: {beat.narration}
visual: {beat.visual_hint}

{_tag_block("Art direction tags:", request.visual_tags())}{_instructions_block(request, scope="images")}
30-60 words, one paragraph, only visible detail, no text in the image,
composed for a {request.aspect_ratio} frame, no aspect ratio and no prefix.

JSON shape:
{{"prompts": [{{"index": {beat.index}, "prompt": "..."}}]}}

{_footer("image_prompts", 1, beat.index)}"""
