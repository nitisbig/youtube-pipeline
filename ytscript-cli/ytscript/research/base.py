"""Research backends (the seam for web research later).

The pipeline already runs a research stage and feeds ``ResearchResult.notes``
into the outline and beat prompts. Shipping backends:

* ``none`` - no-op (default)
* ``file`` - read local notes with ``--research-file notes.md``

To add web research, register a backend that implements ``gather``; no other
file needs to change. See docs/EXTENDING.md.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any, Dict, List

from ..models import ScriptRequest
from ..registry import Registry
from ..utils import get_logger, read_text


@dataclass
class ResearchResult:
    notes: str = ""
    sources: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"notes": self.notes, "sources": self.sources, "metadata": self.metadata}


class ResearchProvider(abc.ABC):
    name = "base"

    @abc.abstractmethod
    def gather(self, request: ScriptRequest) -> ResearchResult:
        """Return grounding notes for the topic."""


RESEARCH: Registry = Registry("research backend")


@RESEARCH.register("none")
class NullResearch(ResearchProvider):
    """Default: rely on the model's own knowledge."""

    name = "none"

    def __init__(self, **_: Any) -> None:
        pass

    def gather(self, request: ScriptRequest) -> ResearchResult:
        return ResearchResult()


@RESEARCH.register("file")
class FileResearch(ResearchProvider):
    """Ground the script in your own notes: ``--research file --research-file X``."""

    name = "file"

    def __init__(self, path: str = "", max_chars: int = 8000, **_: Any) -> None:
        self.path = path
        self.max_chars = max_chars

    def gather(self, request: ScriptRequest) -> ResearchResult:
        if not self.path:
            get_logger().warning("--research file needs --research-file PATH; skipping")
            return ResearchResult()
        text = read_text(self.path)[: self.max_chars]
        return ResearchResult(notes=text, sources=[self.path], metadata={"chars": len(text)})


def get_research(name: str, **kwargs: Any) -> ResearchProvider:
    return RESEARCH.get(name or "none")(**kwargs)
