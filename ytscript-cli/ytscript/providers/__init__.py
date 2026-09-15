"""Provider plugins.

Drop a new module in this package, decorate the class with
``@PROVIDERS.register("my_name")`` and import it below (or register it from
your own code). Nothing else changes.
"""

from .base import PROVIDERS, CompletionRequest, LLMProvider, get_provider

# Built-in providers (import = registration)
from . import anthropic_messages  # noqa: F401,E402
from . import mock  # noqa: F401,E402
from . import openai_compatible  # noqa: F401,E402

__all__ = [
    "PROVIDERS",
    "CompletionRequest",
    "LLMProvider",
    "get_provider",
]
