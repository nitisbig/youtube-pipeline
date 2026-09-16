"""OpenAI-compatible ``/chat/completions`` provider.

Works unchanged with OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral,
Fireworks, vLLM, LM Studio, Ollama (``/v1``) and anything else that speaks the
same shape. Point ``base_url`` at the vendor and set ``model``.
"""

from __future__ import annotations

from typing import Any, Dict

from ..errors import ConfigError, ProviderError
from .base import PROVIDERS, CompletionRequest, LLMProvider
from .http import post_json

PLACEHOLDERS = {
    "",
    "none",
    "replace_me",
    "your_api_key_here",
    "replace_with_api_key",
    "sk-xxxx",
}


@PROVIDERS.register("openai_compatible")
class OpenAICompatibleProvider(LLMProvider):
    name = "openai_compatible"
    supports_json_mode = True

    def _endpoint(self) -> str:
        base = (self.settings.base_url or "").rstrip("/")
        if not base:
            raise ConfigError("base_url is not set (settings.json or YTSCRIPT_BASE_URL)")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions"

    def _check_credentials(self) -> None:
        key = (self.settings.api_key or "").strip()
        if key.lower() in PLACEHOLDERS or key.startswith("REPLACE_"):
            raise ConfigError(
                "no API key configured. Set YTSCRIPT_API_KEY in .env or api_key in "
                "settings.json (or run with --provider mock to try the tool offline)."
            )
        model = (self.settings.model or "").strip()
        if not model or model.startswith("REPLACE_"):
            raise ConfigError("no model configured. Set YTSCRIPT_MODEL or provider.model.")

    def complete(self, request: CompletionRequest) -> str:
        self._check_credentials()

        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})

        payload: Dict[str, Any] = {
            "model": self.settings.model,
            "messages": messages,
            "temperature": (
                request.temperature if request.temperature is not None else self.settings.temperature
            ),
            "max_tokens": request.max_tokens or self.settings.max_tokens,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        if request.seed is not None:
            payload["seed"] = request.seed
        payload.update(self.settings.extra_body)

        headers = {
            "authorization": f"Bearer {self.settings.api_key}",
            **self.settings.extra_headers,
        }

        data = post_json(
            self._endpoint(),
            payload,
            headers,
            timeout=self.settings.timeout,
            retries=self.settings.retries,
        )
        return _extract_text(data)


def _extract_text(data: Dict[str, Any]) -> str:
    try:
        choice = data["choices"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderError(f"unexpected response shape: {str(data)[:300]}") from exc

    message = choice.get("message") or {}
    content = message.get("content")
    if isinstance(content, list):  # some gateways return content parts
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    content = content or choice.get("text") or ""
    if not content.strip():
        raise ProviderError("provider returned an empty completion")
    return content
