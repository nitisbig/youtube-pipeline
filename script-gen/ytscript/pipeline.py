"""The pipeline: an ordered list of swappable stages sharing one context.

    research -> outline -> beats -> image_prompts -> validate

Add, remove or reorder stages without touching the CLI::

    Pipeline.default().insert_after("beats", FactCheckStage()).remove("research")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

from .config import Settings
from .errors import ValidationError
from .generators import BeatGenerator, ImagePromptGenerator, OutlineGenerator
from .models import Beat, Outline, Script, ScriptRequest
from .providers.base import LLMProvider, get_provider
from .research import ResearchProvider, ResearchResult, get_research
from .utils import get_logger, word_count


# --------------------------------------------------------------------------- #
# Shared state
# --------------------------------------------------------------------------- #


@dataclass
class Context:
    """Mutable state passed through the stages."""

    request: ScriptRequest
    provider: LLMProvider
    research: ResearchProvider
    settings: Settings
    outline: Outline = field(default_factory=Outline)
    beats: List[Beat] = field(default_factory=list)
    research_result: ResearchResult = field(default_factory=ResearchResult)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def script(self) -> Script:
        return Script(
            topic=self.request.topic,
            title=self.outline.title or self.request.title or self.request.topic,
            beats=self.beats,
            outline=self.outline,
            metadata=self.metadata,
        )


class Stage(Protocol):
    name: str

    def run(self, context: Context) -> None: ...


# --------------------------------------------------------------------------- #
# Stages
# --------------------------------------------------------------------------- #


class ResearchStage:
    name = "research"

    def run(self, context: Context) -> None:
        result = context.research.gather(context.request)
        context.research_result = result
        if result.notes:
            get_logger().info(
                "research: %d chars from %s", len(result.notes), context.research.name
            )
        context.metadata["research"] = {
            "backend": context.research.name,
            "sources": result.sources,
        }


class OutlineStage:
    name = "outline"

    def run(self, context: Context) -> None:
        context.outline = OutlineGenerator(context.provider).generate(
            context.request, context.research_result.notes
        )


class BeatStage:
    name = "beats"

    def run(self, context: Context) -> None:
        context.beats = BeatGenerator(context.provider).generate(
            context.request, context.outline
        )


class ImagePromptStage:
    name = "image_prompts"

    def run(self, context: Context) -> None:
        context.beats = ImagePromptGenerator(context.provider).generate(
            context.request, context.beats
        )


class ValidateStage:
    """Guards the invariants so bad output is never written to disk."""

    name = "validate"

    def run(self, context: Context) -> None:
        request, beats = context.request, context.beats
        expected = request.beat_count

        if len(beats) != expected:
            raise ValidationError(f"expected {expected} beats, got {len(beats)}")

        missing_narration = [b.index for b in beats if not b.narration.strip()]
        if missing_narration:
            raise ValidationError(f"beats without narration: {missing_narration}")

        missing_prompts = [b.index for b in beats if not b.image_prompt.strip()]
        if missing_prompts:
            raise ValidationError(f"beats without an image prompt: {missing_prompts}")

        if [b.index for b in beats] != list(range(1, expected + 1)):
            raise ValidationError("beat indexes are not a clean 1..N sequence")

        words = sum(word_count(b.narration) for b in beats)
        context.metadata.update(
            {
                "beat_count": len(beats),
                "image_prompt_count": len(beats),
                "word_count": words,
                "estimated_minutes": round(words / max(1, request.words_per_minute), 2),
                "provider": context.provider.describe(),
                "aspect_ratio": request.aspect_ratio,
                "image_prefix": request.image_prefix,
            }
        )


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


class Pipeline:
    def __init__(self, stages: Optional[List[Stage]] = None) -> None:
        self.stages: List[Stage] = list(stages or [])

    @classmethod
    def default(cls) -> "Pipeline":
        return cls(
            [ResearchStage(), OutlineStage(), BeatStage(), ImagePromptStage(), ValidateStage()]
        )

    # --- composition ---------------------------------------------------- #
    def add(self, stage: Stage) -> "Pipeline":
        self.stages.append(stage)
        return self

    def insert_after(self, name: str, stage: Stage) -> "Pipeline":
        for position, existing in enumerate(self.stages):
            if existing.name == name:
                self.stages.insert(position + 1, stage)
                return self
        return self.add(stage)

    def remove(self, name: str) -> "Pipeline":
        self.stages = [s for s in self.stages if s.name != name]
        return self

    def names(self) -> List[str]:
        return [s.name for s in self.stages]

    # --- execution ------------------------------------------------------ #
    def run(self, context: Context) -> Script:
        log = get_logger()
        for stage in self.stages:
            log.debug("stage: %s", stage.name)
            stage.run(context)
        return context.script()


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #


def build_context(
    request: ScriptRequest,
    settings: Optional[Settings] = None,
    research_kwargs: Optional[Dict[str, Any]] = None,
) -> Context:
    settings = settings or Settings()
    return Context(
        request=request,
        provider=get_provider(settings.provider),
        research=get_research(request.research or settings.research, **(research_kwargs or {})),
        settings=settings,
    )


def generate_script(
    request: ScriptRequest,
    settings: Optional[Settings] = None,
    pipeline: Optional[Pipeline] = None,
    research_kwargs: Optional[Dict[str, Any]] = None,
) -> Script:
    """One-call API for other tools and agents."""
    context = build_context(request, settings, research_kwargs)
    return (pipeline or Pipeline.default()).run(context)
