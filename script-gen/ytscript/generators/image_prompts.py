"""Image prompt generator.

Hard invariant: **one prompt per beat**. The generator enforces it with three
layers - chunked generation, a single-item repair call, then a local
synthesiser built from the beat's visual hint plus the art-direction tags.
Every prompt is forced through ``ensure_prefix`` so it starts with exactly one
``create image with 16:9 ratio.`` prefix.
"""

from __future__ import annotations

from typing import Dict, List

from ..models import Beat, ScriptRequest
from ..prompts import SYSTEM_WRITER, image_prompts_prompt, single_image_prompt
from ..providers.base import LLMProvider
from ..utils import ensure_prefix, extract_json, get_logger, squeeze
from .beats import _as_items


class ImagePromptGenerator:
    name = "image_prompts"

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.log = get_logger()

    # ------------------------------------------------------------------ #
    def generate(self, request: ScriptRequest, beats: List[Beat]) -> List[Beat]:
        prefix = request.image_prefix
        chunk = max(1, min(request.chunk_size, len(beats) or 1))

        for offset in range(0, len(beats), chunk):
            window = beats[offset : offset + chunk]
            self.log.info(
                "writing image prompts %d-%d of %d",
                window[0].index,
                window[-1].index,
                len(beats),
            )
            try:
                raw = self.provider.ask(
                    image_prompts_prompt(request, window),
                    system=SYSTEM_WRITER,
                    json_mode=True,
                    seed=request.seed,
                )
                produced = self._parse(raw, window)
            except Exception as exc:  # noqa: BLE001
                self.log.warning("image prompt chunk failed (%s), repairing individually", exc)
                produced = {}

            for beat in window:
                prompt = produced.get(beat.index) or self._repair_missing(request, beat)
                beat.image_prompt = ensure_prefix(prompt, prefix)

        return beats

    # ------------------------------------------------------------------ #
    def _parse(self, raw: str, window: List[Beat]) -> Dict[int, str]:
        data = extract_json(raw)
        items = _as_items(data, "prompts")
        valid = {beat.index for beat in window}
        parsed: Dict[int, str] = {}

        for position, item in enumerate(items):
            if isinstance(item, str):
                text, index = item, window[min(position, len(window) - 1)].index
            elif isinstance(item, dict):
                text = str(item.get("prompt") or item.get("text") or "")
                try:
                    index = int(item.get("index", window[min(position, len(window) - 1)].index))
                except (TypeError, ValueError):
                    index = window[min(position, len(window) - 1)].index
            else:
                continue

            text = squeeze(text)
            if not text:
                continue
            if index not in valid:
                index = window[min(position, len(window) - 1)].index
            parsed.setdefault(index, text)
        return parsed

    def _repair_missing(self, request: ScriptRequest, beat: Beat) -> str:
        self.log.warning("image prompt %d missing, requesting a repair", beat.index)
        try:
            raw = self.provider.ask(
                single_image_prompt(request, beat),
                system=SYSTEM_WRITER,
                json_mode=True,
                seed=request.seed,
            )
            parsed = self._parse(raw, [beat])
            if parsed.get(beat.index):
                return parsed[beat.index]
        except Exception as exc:  # noqa: BLE001
            self.log.debug("image repair failed: %s", exc)
        return self._synthesise(request, beat)

    @staticmethod
    def _synthesise(request: ScriptRequest, beat: Beat) -> str:
        """Last-resort local prompt so the counts can never diverge."""
        subject = beat.visual_hint or beat.narration
        parts = [
            squeeze(subject),
            request.image_style,
            request.art_style,
            request.lighting,
            request.palette,
            request.camera,
            request.composition,
            request.subject_lock,
            "no text, no watermarks, no letters",
        ]
        return ", ".join(p for p in parts if p)
