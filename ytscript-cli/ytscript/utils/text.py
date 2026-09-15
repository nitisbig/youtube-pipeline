"""Text helpers: JSON salvage, slugs, cleanup."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from ..errors import ValidationError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(raw: str) -> Any:
    """Parse JSON out of an LLM response, tolerating fences and prose."""
    if raw is None:
        raise ValidationError("empty model response")
    text = raw.strip()
    if not text:
        raise ValidationError("empty model response")

    candidates = _FENCE.findall(text) + [text]

    for start, end in (("{", "}"), ("[", "]")):
        i, j = text.find(start), text.rfind(end)
        if i != -1 and j > i:
            candidates.append(text[i : j + 1])

    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue

    raise ValidationError(f"model did not return valid JSON: {text[:200]!r}")


def slugify(value: str, default: str = "script") -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii").lower()
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value or default


def squeeze(value: str) -> str:
    """Collapse whitespace so one beat is always one clean line."""
    return re.sub(r"\s+", " ", (value or "")).strip()


def strip_beat_label(value: str) -> str:
    """Remove any leading ``beat 12 ->`` / ``12.`` label a model added."""
    value = squeeze(value)
    value = re.sub(
        r"^beat\s*#?\s*\d+\s*(?:[:.\-\u2013\u2014]|->|\u2192)?\s*", "", value, flags=re.I
    )
    value = re.sub(r"^\d+\s*[).:\-]\s*", "", value)
    return value.strip()


def ensure_prefix(prompt: str, prefix: str) -> str:
    """Guarantee an image prompt starts with exactly one required prefix."""
    body = squeeze(prompt)
    prefix = squeeze(prefix)
    if not prefix:
        return body
    lowered = body.lower()
    # Drop a prefix the model already echoed.
    while lowered.startswith(prefix.lower()):
        body = body[len(prefix) :].strip(" .")
        lowered = body.lower()
    body = re.sub(r"^create (?:an )?image with [^.]*\.\s*", "", body, flags=re.I)
    return f"{prefix} {body}".strip()


def word_count(value: str) -> int:
    return len(squeeze(value).split())
