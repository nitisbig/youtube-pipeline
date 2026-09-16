"""Layered, provider-agnostic configuration.

Precedence (highest wins)::

    CLI flags  >  process env  >  .env file  >  settings.json  >  built-in defaults

Nothing in this file knows about a specific vendor: a provider is just a name
plus ``api_key`` / ``model`` / ``base_url``.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from .errors import ConfigError

ENV_PREFIX = "YTSCRIPT_"

SETTINGS_CANDIDATES = ("settings.json", ".ytscript.json")
USER_SETTINGS = Path.home() / ".config" / "ytscript" / "settings.json"
ENV_CANDIDATES = (".env",)

# Generic vendor env vars we accept as a courtesy fallback.
FALLBACK_API_KEYS = ("OPENAI_API_KEY", "LLM_API_KEY", "API_KEY")
FALLBACK_BASE_URLS = ("OPENAI_BASE_URL", "LLM_BASE_URL", "BASE_URL")
FALLBACK_MODELS = ("OPENAI_MODEL", "LLM_MODEL", "MODEL")


# --------------------------------------------------------------------------- #
# dotenv (no third-party dependency)
# --------------------------------------------------------------------------- #


def parse_dotenv(text: str) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.lower().startswith("export "):
            line = line[7:]
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            values[key] = value
    return values


def load_dotenv(path: Optional[Path] = None, override: bool = False) -> Dict[str, str]:
    """Load a ``.env`` file into ``os.environ`` and return what it contained."""
    paths: List[Path] = [path] if path else [Path.cwd() / name for name in ENV_CANDIDATES]
    loaded: Dict[str, str] = {}
    for candidate in paths:
        if not candidate or not candidate.is_file():
            if path:
                raise ConfigError(f"--env-file not found: {candidate}")
            continue
        values = parse_dotenv(candidate.read_text(encoding="utf-8"))
        for key, value in values.items():
            if override or key not in os.environ:
                os.environ[key] = value
        loaded.update(values)
        break
    return loaded


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass
class ProviderSettings:
    """Vendor-neutral LLM endpoint description."""

    name: str = "openai_compatible"
    api_key: Optional[str] = None
    model: str = "REPLACE_WITH_MODEL_NAME"
    base_url: str = "https://api.openai.com/v1"
    temperature: float = 0.8
    max_tokens: int = 4096
    timeout: int = 120
    retries: int = 2
    extra_headers: Dict[str, str] = field(default_factory=dict)
    extra_body: Dict[str, Any] = field(default_factory=dict)

    def redacted(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.api_key:
            data["api_key"] = (
                f"{self.api_key[:4]}\u2026{self.api_key[-2:]}" if len(self.api_key) > 8 else "set"
            )
        return data


@dataclass
class Settings:
    """Resolved configuration for one run."""

    provider: ProviderSettings = field(default_factory=ProviderSettings)
    defaults: Dict[str, Any] = field(default_factory=dict)
    profiles: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    output: Dict[str, Any] = field(default_factory=dict)
    research: str = "none"
    sources: List[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    @classmethod
    def load(
        cls,
        config_path: Optional[str] = None,
        env_file: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> "Settings":
        settings = cls()

        if env is None:
            load_dotenv(Path(env_file) if env_file else None)
            env = dict(os.environ)

        # 1. settings.json
        path = _resolve_settings_path(config_path)
        if path:
            settings._apply_file(path)
            settings.sources.append(str(path))

        # 2. environment
        settings._apply_env(env)
        return settings

    # ------------------------------------------------------------------ #
    def _apply_file(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"invalid settings file {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"settings file {path} must contain a JSON object")

        provider = raw.get("provider")
        if isinstance(provider, str):  # shorthand: {"provider": "mock"}
            self.provider.name = provider
        elif isinstance(provider, dict):
            self._apply_provider_dict(provider)

        # flat form is also accepted for convenience
        flat_keys = (
            "api_key",
            "model",
            "base_url",
            "temperature",
            "max_tokens",
            "timeout",
            "retries",
        )
        self._apply_provider_dict({k: raw[k] for k in flat_keys if k in raw})

        self.defaults.update(raw.get("defaults") or {})
        self.profiles.update(raw.get("profiles") or {})
        self.output.update(raw.get("output") or {})
        if raw.get("research"):
            self.research = str(raw["research"])

    def _apply_provider_dict(self, data: Dict[str, Any]) -> None:
        mapping = {
            "name": str,
            "api_key": str,
            "model": str,
            "base_url": str,
            "temperature": float,
            "max_tokens": int,
            "timeout": int,
            "retries": int,
        }
        for key, caster in mapping.items():
            if data.get(key) not in (None, ""):
                setattr(self.provider, key, caster(data[key]))
        if isinstance(data.get("extra_headers"), dict):
            self.provider.extra_headers.update(data["extra_headers"])
        if isinstance(data.get("extra_body"), dict):
            self.provider.extra_body.update(data["extra_body"])

    def _apply_env(self, env: Dict[str, str]) -> None:
        def pick(name: str, *fallbacks: str) -> Optional[str]:
            value = env.get(ENV_PREFIX + name)
            if value:
                return value
            for fallback in fallbacks:
                if env.get(fallback):
                    return env[fallback]
            return None

        data: Dict[str, Any] = {
            "name": pick("PROVIDER"),
            "api_key": pick("API_KEY", *FALLBACK_API_KEYS),
            "model": pick("MODEL", *FALLBACK_MODELS),
            "base_url": pick("BASE_URL", *FALLBACK_BASE_URLS),
            "temperature": pick("TEMPERATURE"),
            "max_tokens": pick("MAX_TOKENS"),
            "timeout": pick("TIMEOUT"),
            "retries": pick("RETRIES"),
        }
        self._apply_provider_dict({k: v for k, v in data.items() if v is not None})

        if pick("OUTDIR"):
            self.output["outdir"] = pick("OUTDIR")
        if pick("RESEARCH"):
            self.research = str(pick("RESEARCH"))

    # ------------------------------------------------------------------ #
    def profile(self, name: Optional[str]) -> Dict[str, Any]:
        """Return a named bundle of tag defaults from ``settings.json``."""
        if not name:
            return {}
        if name not in self.profiles:
            raise ConfigError(
                f"unknown profile {name!r}. available: {', '.join(sorted(self.profiles)) or 'none'}"
            )
        return dict(self.profiles[name])

    def tag_defaults(self, profile: Optional[str] = None) -> Dict[str, Any]:
        merged = dict(self.defaults)
        merged.update(self.profile(profile))
        return merged

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider.redacted(),
            "defaults": self.defaults,
            "profiles": sorted(self.profiles),
            "output": self.output,
            "research": self.research,
            "sources": self.sources,
        }


def _resolve_settings_path(config_path: Optional[str]) -> Optional[Path]:
    if config_path:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ConfigError(f"--config not found: {path}")
        return path
    for name in SETTINGS_CANDIDATES:
        candidate = Path.cwd() / name
        if candidate.is_file():
            return candidate
    return USER_SETTINGS if USER_SETTINGS.is_file() else None
