"""Generators: one model-facing job each, each guaranteeing its own shape."""

from .beats import BeatGenerator
from .image_prompts import ImagePromptGenerator
from .outline import OutlineGenerator

__all__ = ["BeatGenerator", "ImagePromptGenerator", "OutlineGenerator"]
