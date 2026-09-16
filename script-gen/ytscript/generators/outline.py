"""Outline generator: topic + tags -> title, logline, sections."""

from __future__ import annotations

from ..models import Outline, ScriptRequest
from ..prompts import SYSTEM_WRITER, outline_prompt
from ..providers.base import LLMProvider
from ..utils import extract_json, get_logger, squeeze


class OutlineGenerator:
    """Cheap first pass that gives the beat writer a spine to follow."""

    name = "outline"

    def __init__(self, provider: LLMProvider) -> None:
        self.provider = provider
        self.log = get_logger()

    def generate(self, request: ScriptRequest, research_notes: str = "") -> Outline:
        self.log.info("planning outline for %r (%d beats)", request.topic, request.beat_count)
        raw = self.provider.ask(
            outline_prompt(request, research_notes),
            system=SYSTEM_WRITER,
            json_mode=True,
            seed=request.seed,
        )
        try:
            data = extract_json(raw)
        except Exception as exc:  # outline is advisory: never fail the run
            self.log.warning("outline unparseable (%s), continuing without it", exc)
            return Outline(
                title=request.title or request.topic,
                logline="",
                sections=[],
                research_notes=research_notes,
            )

        sections = [s for s in (data.get("sections") or []) if isinstance(s, dict)]
        return Outline(
            title=request.title or squeeze(str(data.get("title") or request.topic)),
            logline=squeeze(str(data.get("logline") or "")),
            sections=sections,
            research_notes=research_notes,
        )
