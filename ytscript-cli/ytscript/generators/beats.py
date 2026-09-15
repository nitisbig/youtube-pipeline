"""Beat generator.

Beats are produced in chunks (``--chunk-size``) with the tail of the previous
chunk replayed for continuity. This keeps long scripts (90+ beats) reliable
and inside any context window.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..errors import ValidationError
from ..models import Beat, Outline, ScriptRequest
from ..prompts import SYSTEM_WRITER, beats_prompt, single_beat_prompt
from ..providers.base import LLMProvider
from ..utils import extract_json, get_logger, squeeze, strip_beat_label


class BeatGenerator:
    name = "beats"

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.log = get_logger()

    # ------------------------------------------------------------------ #
    def generate(self, request: ScriptRequest, outline: Outline) -> List[Beat]:
        total = request.beat_count
        chunk = max(1, min(request.chunk_size, total))
        beats: List[Beat] = []

        while len(beats) < total:
            start = len(beats) + 1
            count = min(chunk, total - len(beats))
            self.log.info("writing beats %d-%d of %d", start, start + count - 1, total)

            raw = self.provider.ask(
                beats_prompt(request, outline, start, count, beats),
                system=SYSTEM_WRITER,
                json_mode=True,
                seed=request.seed,
            )
            produced = self._parse(raw, start, count)

            for offset in range(count):
                index = start + offset
                beat = produced.get(index)
                if beat is None:
                    beat = self._repair_beat(request, outline, index)
                beats.append(beat)

        return self._renumber(beats[:total])

    # ------------------------------------------------------------------ #
    def _parse(self, raw: str, start: int, count: int) -> Dict[int, Beat]:
        data = extract_json(raw)
        items = _as_items(data, "beats")
        parsed: Dict[int, Beat] = {}

        for position, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            narration = strip_beat_label(str(item.get("narration") or item.get("text") or ""))
            if not narration:
                continue
            try:
                index = int(item.get("index", start + position))
            except (TypeError, ValueError):
                index = start + position
            if not start <= index < start + count:
                index = start + position
            if index in parsed or not start <= index < start + count:
                continue
            parsed[index] = Beat(
                index=index,
                narration=narration,
                visual_hint=squeeze(str(item.get("visual_hint") or item.get("visual") or "")),
            )
        return parsed

    def _repair_beat(self, request: ScriptRequest, outline: Outline, index: int) -> Beat:
        """One targeted retry for a single missing beat."""
        self.log.warning("beat %d missing, requesting a repair", index)
        try:
            raw = self.provider.ask(
                single_beat_prompt(request, outline, index),
                system=SYSTEM_WRITER,
                json_mode=True,
                seed=request.seed,
            )
            parsed = self._parse(raw, index, 1)
            if index in parsed:
                return parsed[index]
        except Exception as exc:  # noqa: BLE001 - fall through to a hard error
            self.log.debug("repair call failed: %s", exc)
        raise ValidationError(
            f"could not generate beat {index}; try a lower --chunk-size or a stronger model"
        )

    @staticmethod
    def _renumber(beats: List[Beat]) -> List[Beat]:
        for position, beat in enumerate(beats, start=1):
            beat.index = position
        return beats


def _as_items(data: Any, key: str) -> List[Any]:
    if isinstance(data, dict):
        for candidate in (key, "items", "data", "result"):
            value = data.get(candidate)
            if isinstance(value, list):
                return value
        return []
    return data if isinstance(data, list) else []
