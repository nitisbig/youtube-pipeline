"""Small, dependency-free helpers."""

from .io import read_text, write_text
from .logging import get_logger, setup_logging
from .text import (
    ensure_prefix,
    extract_json,
    slugify,
    squeeze,
    strip_beat_label,
    word_count,
)

__all__ = [
    "read_text",
    "write_text",
    "get_logger",
    "setup_logging",
    "ensure_prefix",
    "extract_json",
    "slugify",
    "squeeze",
    "strip_beat_label",
    "word_count",
]
