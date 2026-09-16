"""Tiny generic registry used to make every moving part plug-and-play.

Providers, research backends and renderers are all registered here, so adding
a new one never requires touching the CLI or the pipeline::

    from ytscript.providers import PROVIDERS

    @PROVIDERS.register("my_llm")
    class MyProvider(LLMProvider):
        ...
"""

from __future__ import annotations

from typing import Dict, Generic, Iterable, List, Tuple, TypeVar

from .errors import ConfigError

T = TypeVar("T")


class Registry(Generic[T]):
    """Name -> factory map with a decorator-based registration API."""

    def __init__(self, label: str) -> None:
        self.label = label
        self._items: Dict[str, T] = {}

    def register(self, name: str, obj=None):
        """Register ``obj`` under ``name``. Usable as a decorator."""
        if obj is not None:
            self._items[name] = obj
            return obj

        def decorator(target: T) -> T:
            self._items[name] = target
            return target

        return decorator

    def get(self, name: str) -> T:
        try:
            return self._items[name]
        except KeyError:
            raise ConfigError(
                f"unknown {self.label} {name!r}. available: {', '.join(self.names()) or 'none'}"
            ) from None

    def names(self) -> List[str]:
        return sorted(self._items)

    def items(self) -> Iterable[Tuple[str, T]]:
        return sorted(self._items.items())

    def __contains__(self, name: object) -> bool:
        return name in self._items


__all__ = ["Registry"]
