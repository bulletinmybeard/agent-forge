"""Brave Search API provider.

Brave's Web Search endpoint:

    GET https://api.search.brave.com/res/v1/web/search?q=...

Auth header: ``X-Subscription-Token: <api_key>``.

Brave has no /fetch equivalent — only search lives here. ``web_fetch``
falls back to Ollama or Tavily depending on ``tools.web_fetch.provider``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from chalkbox.logging.bridge import get_logger

from .base import ProviderError, ProviderNotConfigured, RateLimitError, SearchResult

logger = get_logger(__name__)


_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_REQUEST_TIMEOUT = 30


def _get_api_key() -> str:
    """``BRAVE_API_KEY`` env wins, else ``tools.web_search.brave.api_key`` (YAML)."""
    env = os.environ.get("BRAVE_API_KEY", "").strip()
    if env:
        return env
    try:
        from agentforge.config import get_config

        cfg = get_config()
        key = cfg._raw.get("tools", {}).get("web_search", {}).get("brave", {}).get("api_key", "")
        if key:
            return str(key)
    except Exception:
        pass
    return ""


def _get_brave_options() -> dict[str, Any]:
    """Per-deployment knobs (country/search_lang/safesearch). All optional."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return dict(cfg._raw.get("tools", {}).get("web_search", {}).get("brave", {}) or {})
    except Exception:
        return {}


class BraveSearchProvider:
    name = "brave"

    def is_available(self) -> bool:
        return bool(_get_api_key())

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        api_key = _get_api_key()
        if not api_key:
            raise ProviderNotConfigured(
                "brave search: no API key (set BRAVE_API_KEY or tools.web_search.brave.api_key in config.yaml)"
            )

        opts = _get_brave_options()
        # Brave caps count at 20; max_results from our side is already
        # clamped before getting here, but enforce again defensively.
        count = max(1, min(int(max_results), 20))
        params: dict[str, Any] = {"q": query, "count": count}
        # Optional refinements (all skipped when empty / missing).
        for key in ("country", "search_lang", "ui_lang"):
            val = opts.get(key)
            if val:
                params[key] = val
        safesearch = opts.get("safesearch")
        if safesearch:
            params["safesearch"] = safesearch

        try:
            resp = httpx.get(
                _SEARCH_URL,
                params=params,
                headers={
                    "X-Subscription-Token": api_key,
                    "Accept": "application/json",
                },
                timeout=_REQUEST_TIMEOUT,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"brave network error: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"brave rate limited: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise ProviderError(f"brave server error (HTTP {resp.status_code}): {resp.text[:200]}")
        if resp.status_code >= 400:
            raise ProviderError(f"brave API error (HTTP {resp.status_code}): {resp.text[:200]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(f"brave returned non-JSON: {resp.text[:200]}") from exc

        web_block = data.get("web") or {}
        raw_results = web_block.get("results") or []

        out: list[SearchResult] = []
        for r in raw_results:
            # Brave's snippet field is `description`. `extra_snippets` may
            # carry extra context — when present, fold the first one in so
            # the LLM gets a richer snippet without changing the contract.
            description = (r.get("description") or "").strip()
            extras = r.get("extra_snippets") or []
            if extras and len(description) < 200:
                first_extra = str(extras[0]).strip()
                if first_extra and first_extra not in description:
                    description = (description + " " + first_extra).strip()

            out.append(
                SearchResult(
                    title=str(r.get("title") or "(no title)"),
                    url=str(r.get("url") or ""),
                    content=description,
                    age=(str(r.get("age")) if r.get("age") else None),
                )
            )
        return out


__all__ = ["BraveSearchProvider"]
