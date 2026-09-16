"""ytscript - modular, provider-agnostic YouTube script generator.

Quick programmatic use::

    from ytscript import ScriptRequest, Settings, generate_script, render_all, RenderOptions

    script = generate_script(ScriptRequest(topic="Dog Psychology", beats=90), Settings.load())
    files = render_all(script, RenderOptions())
"""

from .config import ProviderSettings, Settings
from .errors import (
    ConfigError,
    OutputError,
    ProviderError,
    UsageError,
    ValidationError,
    YtScriptError,
)
from .models import Beat, Outline, Script, ScriptRequest
from .pipeline import Context, Pipeline, build_context, generate_script
from .renderers import RenderOptions, render_all

__version__ = "1.0.0"

__all__ = [
    "__version__",
    "Beat",
    "ConfigError",
    "Context",
    "Outline",
    "OutputError",
    "Pipeline",
    "ProviderError",
    "ProviderSettings",
    "RenderOptions",
    "Script",
    "ScriptRequest",
    "Settings",
    "UsageError",
    "ValidationError",
    "YtScriptError",
    "build_context",
    "generate_script",
    "render_all",
]
