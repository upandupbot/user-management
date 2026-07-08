"""Thin GraphQL wrapper around the Pipefy API with retry/backoff."""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

try:
    import requests  # type: ignore
except ImportError:  # pragma: no cover - requests is in requirements
    requests = None  # type: ignore


log = logging.getLogger(__name__)


class PipefyError(RuntimeError):
    pass


class RateLimitError(PipefyError):
    pass


class PipefyClient:
    """Minimal GraphQL client. Retries on 429 / rate-limit messages."""

    def __init__(
        self,
        api_url: str,
        api_token: str,
        *,
        max_retries: int = 3,
        backoff_base: float = 2.0,
        session: Any = None,
        sleep: Any = time.sleep,
    ) -> None:
        self.api_url = api_url
        self._token = api_token
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self._sleep = sleep
        if session is not None:
            self._session = session
        elif requests is not None:
            self._session = requests.Session()
        else:  # pragma: no cover
            self._session = None

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def execute(self, query: str, variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        payload = {"query": query, "variables": variables or {}}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._session.post(
                    self.api_url,
                    json=payload,
                    headers=self._headers(),
                    timeout=60,
                )
            except Exception as exc:  # network-level
                if attempt > self.max_retries:
                    raise PipefyError(f"Network error after {attempt - 1} retries: {exc}") from exc
                self._sleep(self.backoff_base ** attempt)
                continue

            status = getattr(resp, "status_code", 200)
            if status == 429:
                if attempt > self.max_retries:
                    raise RateLimitError("Rate limited (429) and out of retries")
                self._sleep(self.backoff_base ** attempt)
                continue

            try:
                data = resp.json()
            except Exception as exc:
                raise PipefyError(f"Invalid JSON from Pipefy (status {status}): {exc}")

            errors = data.get("errors") if isinstance(data, dict) else None
            if errors:
                def _err_msg(e):
                    if isinstance(e, dict):
                        return str(e.get("message", e))
                    return str(e)
                # Pipefy sometimes returns `errors` as a bare string instead of a list.
                if isinstance(errors, (str, bytes)) or isinstance(errors, dict):
                    errors = [errors]
                msg = "; ".join(_err_msg(e) for e in errors)
                if any(tok in msg.lower() for tok in ("rate limit", "throttle", "too many")):
                    if attempt > self.max_retries:
                        raise RateLimitError(msg)
                    self._sleep(self.backoff_base ** attempt)
                    continue
                raise PipefyError(msg)

            if not isinstance(data, dict) or "data" not in data:
                raise PipefyError(f"Unexpected response shape: {data!r}")
            return data["data"]
