"""Domain models.

These dataclasses are the contract between the CLI, the pipeline stages and
the renderers. Anything that wants to drive ytscript programmatically (another
agent, a web service, a cron job) only needs :class:`ScriptRequest` in and
:class:`Script` out.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# Request
# --------------------------------------------------------------------------- #


@dataclass
class ScriptRequest:
    """Everything needed to produce a script. All fields are "tags".

    Tags are grouped by concern:

    * structure  - how long / how many beats
    * narration  - voice, tone, audience, language ...
    * visual     - image prompt art direction
    * freeform   - ``extra`` / ``image_extra`` accept arbitrary key=value tags
    """

    # --- required ---------------------------------------------------------- #
    topic: str

    # --- structure --------------------------------------------------------- #
    beats: Optional[int] = None
    duration_minutes: float = 7.0
    words_per_minute: int = 150
    words_per_beat: int = 12

    # --- narration tags ---------------------------------------------------- #
    title: Optional[str] = None
    language: str = "English"
    tone: str = "curious, warm, documentary"
    style: str = "story-driven explainer"
    audience: str = "general YouTube audience"
    narrator: str = "neutral third-person narrator"
    pov: str = "third person"
    hook: str = "open with a concrete curiosity gap in the first two beats"
    cta: Optional[str] = None
    depth: str = "balanced"
    pacing: str = "steady"
    humor: str = "none"
    emotion: Optional[str] = None
    genre: Optional[str] = None
    series: Optional[str] = None
    reading_level: str = "clear, conversational, grade 8"
    keywords: List[str] = field(default_factory=list)
    avoid: List[str] = field(default_factory=list)
    # Free-form producer notes (beat count, structure, narrator voice, facts
    # to include ...). Injected verbatim into every prompt, above the tags.
    instructions: Optional[str] = None

    # --- visual tags ------------------------------------------------------- #
    aspect_ratio: str = "16:9"
    image_style: str = "cinematic realism"
    art_style: Optional[str] = None
    palette: Optional[str] = None
    lighting: Optional[str] = None
    camera: Optional[str] = None
    composition: Optional[str] = None
    subject_lock: Optional[str] = None
    negative_prompt: Optional[str] = None
    image_prefix_template: str = "create image with {aspect_ratio} ratio."

    # --- freeform / custom tags -------------------------------------------- #
    extra: Dict[str, str] = field(default_factory=dict)
    image_extra: Dict[str, str] = field(default_factory=dict)

    # --- generation knobs -------------------------------------------------- #
    seed: Optional[int] = None
    chunk_size: int = 12
    research: str = "none"

    # ------------------------------------------------------------------ #
    @property
    def beat_count(self) -> int:
        """Explicit ``--beats`` wins, otherwise derive from runtime."""
        if self.beats:
            return max(1, int(self.beats))
        words = self.duration_minutes * self.words_per_minute
        return max(1, int(math.ceil(words / max(1, self.words_per_beat))))

    @property
    def image_prefix(self) -> str:
        return self.image_prefix_template.format(
            aspect_ratio=self.aspect_ratio, topic=self.topic
        ).strip()

    def narration_tags(self) -> Dict[str, str]:
        """Flat tag map handed to the narration prompts."""
        tags = {
            "language": self.language,
            "tone": self.tone,
            "style": self.style,
            "audience": self.audience,
            "narrator": self.narrator,
            "point of view": self.pov,
            "hook": self.hook,
            "depth": self.depth,
            "pacing": self.pacing,
            "humor": self.humor,
            "reading level": self.reading_level,
            "emotion": self.emotion,
            "genre": self.genre,
            "series": self.series,
            "call to action": self.cta,
            "keywords to weave in": ", ".join(self.keywords) or None,
            "avoid": ", ".join(self.avoid) or None,
            "words per beat (approx)": str(self.words_per_beat),
        }
        tags.update(self.extra)
        return {k: v for k, v in tags.items() if v}

    def visual_tags(self) -> Dict[str, str]:
        """Flat tag map handed to the image prompt prompts."""
        tags = {
            "aspect ratio": self.aspect_ratio,
            "image style": self.image_style,
            "art style": self.art_style,
            "color palette": self.palette,
            "lighting": self.lighting,
            "camera": self.camera,
            "composition": self.composition,
            "recurring subject to keep consistent": self.subject_lock,
            "must avoid in images": self.negative_prompt,
        }
        tags.update(self.image_extra)
        return {k: v for k, v in tags.items() if v}

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["resolved_beat_count"] = self.beat_count
        return data


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #


@dataclass
class Beat:
    """One narration unit. 1 beat == 1 voiceover line == 1 image prompt."""

    index: int
    narration: str
    visual_hint: str = ""
    image_prompt: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Outline:
    title: str = ""
    logline: str = ""
    sections: List[Dict[str, Any]] = field(default_factory=list)
    research_notes: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class Script:
    """The finished artifact: title + N beats, each with narration and prompt."""

    topic: str
    title: str
    beats: List[Beat] = field(default_factory=list)
    outline: Outline = field(default_factory=Outline)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def beat_count(self) -> int:
        return len(self.beats)

    @property
    def word_count(self) -> int:
        return sum(len(b.narration.split()) for b in self.beats)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "topic": self.topic,
            "title": self.title,
            "beat_count": self.beat_count,
            "word_count": self.word_count,
            "outline": self.outline.to_dict(),
            "beats": [b.to_dict() for b in self.beats],
            "metadata": self.metadata,
        }
