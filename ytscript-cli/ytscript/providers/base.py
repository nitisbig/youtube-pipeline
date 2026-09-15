"""LLM provider contract + registry.

A provider is deliberately tiny: one text-in / text-out method. That is the
only thing the generators need, so swapping vendors (or plugging a local
model, or another agent) is a ~30 line class.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional

from ..config import ProviderSettings
from ..registry import Registry


@dataclass
class CompletionRequest:
    prompt: str
    system: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    json_mode: bool = False
    seed: Optional[int] = None


class LLMProvider(abc.ABC):
    """Base class for every text backend."""

    #: human readable id, also the registry key
    name: str = "base"
    #: set False for providers that cannot honour a JSON response format
    supports_json_mode: bool = True
    #: set False for offline/local providers that need no credentials
    requires_api_key: bool = True

    def __init__(self, settings: ProviderSettings) -> None:
        self.settings = settings

    @abc.abstractmethod
    def complete(self, request: CompletionRequest) -> str:
        """Return the model's text response."""

    # convenience used by the generators
    def ask(
        self,
        prompt: str,
        system: Optional[str] = None,
        json_mode: bool = False,
        seed: Optional[int] = None,
    ) -> str:
        return self.complete(
            CompletionRequest(
                prompt=prompt,
                system=system,
                temperature=self.settings.temperature,
                max_tokens=self.settings.max_tokens,
                json_mode=json_mode and self.supports_json_mode,
                seed=seed,
            )
        )

    def describe(self) -> str:
        return f"{self.name} ({self.settings.model})"


PROVIDERS: Registry = Registry("provider")


def get_provider(settings: ProviderSettings) -> LLMProvider:
    """Instantiate the provider named in ``settings.name``."""
    cls = PROVIDERS.get(settings.name)
    return cls(settings)
