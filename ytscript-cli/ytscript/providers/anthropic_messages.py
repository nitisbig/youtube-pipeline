"""Anthropic ``/v1/messages`` provider.

Included as a second, deliberately different wire format to prove the
provider abstraction is real. Enable with ``--provider anthropic``.
"""

from __future__ import annotations

from typing import Any, Dict

from ..errors import ConfigError, ProviderError
from .base import PROVIDERS, CompletionRequest, LLMProvider
from .http import post_json


@PROVIDERS.register("anthropic")
class AnthropicMessagesProvider(LLMProvider):
    name = "anthropic"
    supports_json_mode = False  # steered by prompt instead

    def _endpoint(self) -> str:
        base = (self.settings.base_url or "https://api.anthropic.com").rstrip("/")
        if base.endswith("/messages"):
            return base
        if base.endswith("/v1"):
            return f"{base}/messages"
        return f"{base}/v1/messages"

    def complete(self, request: CompletionRequest) -> str:
        key = (self.settings.api_key or "").strip()
        if not key or key.startswith("REPLACE_"):
            raise ConfigError("no API key configured for the anthropic provider")

        payload: Dict[str, Any] = {
            "model": self.settings.model,
            "max_tokens": request.max_tokens or self.settings.max_tokens,
            "temperature": (
                request.temperature if request.temperature is not None else self.settings.temperature
            ),
            "messages": [{"role": "user", "content": request.prompt}],
        }
        if request.system:
            payload["system"] = request.system
        payload.update(self.settings.extra_body)

        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            **self.settings.extra_headers,
        }

        data = post_json(
            self._endpoint(),
            payload,
            headers,
            timeout=self.settings.timeout,
            retries=self.settings.retries,
        )
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        if not text.strip():
            raise ProviderError(f"empty completion: {str(data)[:300]}")
        return text
