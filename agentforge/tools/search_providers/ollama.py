"""Ollama Cloud search + fetch providers — extracted from the original
``web_search.py``.

Same wire format (POST JSON with Bearer auth) and same response parsing
as before the provider refactor, so default behaviour is unchanged when
``tools.web_search.provider`` is left at ``ollama`` (the default).
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from chalkbox.logging.bridge import get_logger

from .base import (
    FetchResult,
    ProviderError,
    ProviderNotConfigured,
    RateLimitError,
    SearchResult,
)

logger = get_logger(__name__)


_SEARCH_URL = "https://ollama.com/api/web_search"
_FETCH_URL = "https://ollama.com/api/web_fetch"
_REQUEST_TIMEOUT = 30  # seconds — matches the legacy urllib timeout


def _get_api_key() -> str:
    """Return the Ollama Cloud API key.

    Priority preserved from the original code:
    ``tools.web_search.api_key`` (YAML) → ``OLLAMA_API_KEY`` env →
    empty string. Empty disables the provider gracefully.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        key = cfg._raw.get("tools", {}).get("web_search", {}).get("api_key", "")
        if key:
            return str(key)
    except Exception:
        pass
    return os.environ.get("OLLAMA_API_KEY", "")


def _post(url: str, body: dict[str, Any], api_key: str) -> dict[str, Any]:
    """POST JSON, return parsed response. Map vendor errors to provider
    errors so the fallback chain can advance on rate limits / 5xx.
    """
    try:
        resp = httpx.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=_REQUEST_TIMEOUT,
        )
    except (httpx.TimeoutException, httpx.NetworkError) as exc:
        raise ProviderError(f"ollama network error: {exc}") from exc

    if resp.status_code == 429:
        raise RateLimitError(f"ollama rate limited: {resp.text[:200]}")
    if resp.status_code >= 500:
        raise ProviderError(f"ollama server error (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        # 401/403 etc — bad key, not a transient failure but still
        # worth advancing the fallback chain (the user may have
        # configured a working alternative).
        raise ProviderError(f"ollama API error (HTTP {resp.status_code}): {resp.text[:200]}")

    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(f"ollama returned non-JSON: {resp.text[:200]}") from exc


class OllamaSearchProvider:
    name = "ollama"

    def is_available(self) -> bool:
        return bool(_get_api_key())

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        api_key = _get_api_key()
        if not api_key:
            raise ProviderNotConfigured(
                "ollama search: no API key (set OLLAMA_API_KEY or tools.web_search.api_key in config.yaml)"
            )
        data = _post(_SEARCH_URL, {"query": query, "max_results": max_results}, api_key)
        raw_results = data.get("results") or []
        out: list[SearchResult] = []
        for r in raw_results:
            out.append(
                SearchResult(
                    title=str(r.get("title") or "(no title)"),
                    url=str(r.get("url") or ""),
                    content=str(r.get("content") or "").strip(),
                )
            )
        return out


class OllamaFetchProvider:
    name = "ollama"

    def is_available(self) -> bool:
        return bool(_get_api_key())

    def fetch(self, url: str) -> FetchResult:
        api_key = _get_api_key()
        if not api_key:
            raise ProviderNotConfigured(
                "ollama fetch: no API key (set OLLAMA_API_KEY or tools.web_search.api_key in config.yaml)"
            )
        data = _post(_FETCH_URL, {"url": url}, api_key)
        return FetchResult(
            title=str(data.get("title") or ""),
            url=url,
            content=str(data.get("content") or ""),
            links=list(data.get("links") or []),
        )


__all__ = ["OllamaSearchProvider", "OllamaFetchProvider"]
