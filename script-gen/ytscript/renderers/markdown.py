"""Renderers: Script -> file contents. Pure functions, no I/O.

Three renderers ship, matching the three required files:

* ``voiceover`` -> ``voiceover.md``      plain narration text only
* ``beats``     -> ``beat.md``           ``beat1 \u2192 narration``
* ``image_prompts`` -> ``image_prompts.md``  prefixed prompts, blank-line separated

Register your own (captions, SRT, shotlist, \u2026) with ``@RENDERERS.register``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

from ..models import Script
from ..registry import Registry
from ..utils import ensure_prefix, squeeze

DEFAULT_FILENAMES = {
    "voiceover": "voiceover.md",
    "beats": "beat.md",
    "image_prompts": "image_prompts.md",
}


@dataclass
class RenderOptions:
    """Everything about the *shape* of the output files."""

    # image_prompts.md
    image_prefix: str = "create image with 16:9 ratio."
    # beat.md
    arrow: str = "\u2192"
    beat_label: str = "beat"
    beat_blank_lines: bool = True
    # voiceover.md
    voiceover_layout: str = "paragraphs"  # paragraphs | lines | single
    paragraph_beats: int = 4
    # filenames
    filenames: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_FILENAMES))


RENDERERS: Registry = Registry("renderer")


@RENDERERS.register("voiceover")
def render_voiceover(script: Script, options: RenderOptions) -> str:
    """Plain narration text. No headings, no numbers, no labels."""
    lines = [squeeze(beat.narration) for beat in script.beats if beat.narration.strip()]

    if options.voiceover_layout == "lines":
        return "\n".join(lines) + "\n"
    if options.voiceover_layout == "single":
        return " ".join(lines) + "\n"

    size = max(1, options.paragraph_beats)
    paragraphs = [" ".join(lines[i : i + size]) for i in range(0, len(lines), size)]
    return "\n\n".join(paragraphs) + "\n"


@RENDERERS.register("beats")
def render_beats(script: Script, options: RenderOptions) -> str:
    """``beat1 \u2192 narration`` \u2014 one entry per beat."""
    separator = "\n\n" if options.beat_blank_lines else "\n"
    entries = [
        f"{options.beat_label}{beat.index} {options.arrow} {squeeze(beat.narration)}"
        for beat in script.beats
    ]
    return separator.join(entries) + "\n"


@RENDERERS.register("image_prompts")
def render_image_prompts(script: Script, options: RenderOptions) -> str:
    """Self-sufficient prompts, one per beat, separated by a blank line."""
    prompts = [
        ensure_prefix(beat.image_prompt or beat.visual_hint, options.image_prefix)
        for beat in script.beats
    ]
    return "\n\n".join(prompts) + "\n"


def render_all(script: Script, options: RenderOptions) -> Dict[str, str]:
    """Run every registered renderer -> ``{filename: content}``."""
    files: Dict[str, str] = {}
    for name, renderer in RENDERERS.items():
        filename = options.filenames.get(name, f"{name}.md")
        files[filename] = renderer(script, options)
    return files
