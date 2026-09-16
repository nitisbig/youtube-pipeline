"""Research plugins (web research drops in here)."""

from .base import RESEARCH, FileResearch, NullResearch, ResearchProvider, ResearchResult, get_research

__all__ = [
    "RESEARCH",
    "FileResearch",
    "NullResearch",
    "ResearchProvider",
    "ResearchResult",
    "get_research",
]
