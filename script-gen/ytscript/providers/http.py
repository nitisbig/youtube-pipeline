"""Minimal JSON-over-HTTP client (stdlib only, retries + backoff)."""

from __future__ import annotations

import json
import random
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from ..errors import ProviderError
from ..utils.logging import get_logger

import re

RETRYABLE_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


def _extract_retry_delay(exc: urllib.error.HTTPError, detail: str) -> Optional[float]:
    match = re.search(r"try again in (\d+(?:\.\d+)?)s", detail, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1)) + 1.0
        except ValueError:
            pass

    if exc.headers:
        for header_name in ("x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
            val = exc.headers.get(header_name)
            if val:
                m = re.search(r"(\d+(?:\.\d+)?)s", val)
                if m:
                    try:
                        return float(m.group(1)) + 1.0
                    except ValueError:
                        pass

        retry_after = exc.headers.get("retry-after")
        if retry_after:
            try:
                return float(retry_after) + 1.0
            except ValueError:
                pass

    return None


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Dict[str, str],
    timeout: int = 120,
    retries: int = 2,
) -> Dict[str, Any]:
    log = get_logger()
    body = json.dumps(payload).encode("utf-8")
    attempt = 0
    last_error: Optional[Exception] = None
    last_status: Optional[int] = None
    retry_delay: Optional[float] = None

    while True:
        max_attempts = max(retries, 5) if last_status == 429 else max(0, retries)
        if attempt > max_attempts:
            break

        request = urllib.request.Request(url, data=body, method="POST")
        request.add_header("content-type", "application/json")
        request.add_header("user-agent", "ytscript/1.0")
        for key, value in headers.items():
            if value:
                request.add_header(key, value)
        try:
            log.debug("POST %s (attempt %d)", url, attempt + 1)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            last_error = ProviderError(f"HTTP {exc.code} from {url}: {detail}")
            last_status = exc.code
            retry_delay = _extract_retry_delay(exc, detail)
            if exc.code not in RETRYABLE_STATUS:
                raise last_error from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = ProviderError(f"request to {url} failed: {exc}")
            last_status = None
            retry_delay = None

        attempt += 1
        max_attempts = max(retries, 5) if last_status == 429 else max(0, retries)
        if attempt <= max_attempts:
            if retry_delay is not None:
                delay = min(65.0, retry_delay)
            else:
                delay = min(30.0, (2**attempt) + random.random())
            log.info("retrying in %.1fs (%s)", delay, last_error)
            time.sleep(delay)

    raise last_error or ProviderError(f"request to {url} failed")
