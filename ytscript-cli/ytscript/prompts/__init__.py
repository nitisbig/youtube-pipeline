"""Prompt templates (the only place model-facing text lives)."""

from .templates import (
    SYSTEM_WRITER,
    beats_prompt,
    image_prompts_prompt,
    outline_prompt,
    single_beat_prompt,
    single_image_prompt,
)

__all__ = [
    "SYSTEM_WRITER",
    "beats_prompt",
    "image_prompts_prompt",
    "outline_prompt",
    "single_beat_prompt",
    "single_image_prompt",
]
