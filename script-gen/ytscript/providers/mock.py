"""Offline deterministic provider.

Purpose: let you run the whole pipeline, the tests and a demo **without any
API key**::

    python3 -m ytscript --topic "Dog Psychology" --provider mock --generate

It reads the machine-readable ``SPEC:`` line the prompt builder appends and
synthesises a correctly shaped JSON payload.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from .base import PROVIDERS, CompletionRequest, LLMProvider

SPEC_RE = re.compile(r"^SPEC:\s*(.*)$", re.MULTILINE)
TOPIC_RE = re.compile(r"^TOPIC:\s*(.*)$", re.MULTILINE)

ANGLES = [
    "the question nobody asks",
    "a small detail with big consequences",
    "what the research actually shows",
    "a myth worth retiring",
    "the mechanism behind it",
    "an everyday example",
    "the counter-intuitive twist",
    "what changes once you know this",
]
SHOTS = [
    "wide establishing shot",
    "slow push-in close-up",
    "overhead flat-lay",
    "low-angle hero shot",
    "side-profile medium shot",
    "macro detail shot",
]


@PROVIDERS.register("mock")
class MockProvider(LLMProvider):
    name = "mock"
    requires_api_key = False
    supports_json_mode = True

    def complete(self, request: CompletionRequest) -> str:
        spec = _parse_spec(request.prompt)
        topic = _match(TOPIC_RE, request.prompt) or "Untitled Topic"
        kind = spec.get("kind", "outline")

        if kind == "outline":
            return json.dumps(_outline(topic))
        if kind == "beats":
            return json.dumps(
                _beats(topic, int(spec.get("count", 1)), int(spec.get("start", 1)))
            )
        if kind == "image_prompts":
            return json.dumps(
                _image_prompts(topic, int(spec.get("count", 1)), int(spec.get("start", 1)))
            )
        return json.dumps({"text": f"mock response about {topic}"})


# --------------------------------------------------------------------------- #


def _match(pattern, text: str) -> str:
    found = pattern.search(text)
    return found.group(1).strip() if found else ""


def _parse_spec(prompt: str) -> Dict[str, str]:
    line = _match(SPEC_RE, prompt)
    spec: Dict[str, str] = {}
    for token in line.split():
        key, _, value = token.partition("=")
        if key and value:
            spec[key] = value
    return spec


def _outline(topic: str) -> Dict[str, Any]:
    return {
        "title": f"{topic}: What We Get Wrong",
        "logline": f"A tight, evidence-minded tour through {topic} and why it matters.",
        "sections": [
            {"name": "Hook", "summary": f"An arresting question about {topic}.", "beats": 6},
            {"name": "Context", "summary": f"Where our ideas about {topic} came from.", "beats": 18},
            {"name": "Mechanism", "summary": f"How {topic} actually works.", "beats": 28},
            {"name": "Implications", "summary": "What follows in daily life.", "beats": 24},
            {"name": "Close", "summary": "A reframe and a parting thought.", "beats": 12},
        ],
    }


def _beats(topic: str, count: int, start: int) -> Dict[str, List[Dict[str, Any]]]:
    beats = []
    for offset in range(count):
        index = start + offset
        angle = ANGLES[index % len(ANGLES)]
        beats.append(
            {
                "index": index,
                "narration": (
                    f"Beat {index}: consider {topic} through {angle}, "
                    "and the picture quietly rearranges itself."
                ),
                "visual_hint": f"{SHOTS[index % len(SHOTS)]} illustrating {angle} in {topic}",
            }
        )
    return {"beats": beats}


def _image_prompts(topic: str, count: int, start: int) -> Dict[str, List[Dict[str, Any]]]:
    prompts = []
    for offset in range(count):
        index = start + offset
        prompts.append(
            {
                "index": index,
                "prompt": (
                    f"{SHOTS[index % len(SHOTS)]} of a scene about {topic} illustrating "
                    f"{ANGLES[index % len(ANGLES)]}, soft directional light, shallow depth of "
                    "field, muted natural palette, photorealistic detail, no text"
                ),
            }
        )
    return {"prompts": prompts}
