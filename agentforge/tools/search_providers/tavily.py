"""Tavily Search + Extract providers.

Two endpoints, both POST JSON with ``Authorization: Bearer tvly-<key>``:

- ``POST https://api.tavily.com/search``  → web search
- ``POST https://api.tavily.com/extract`` → page extraction (web_fetch)

We intentionally leave ``include_answer`` at ``false`` (results-only).
Synthesised answers cost 2 credits per call vs 1 for plain search, and
the abstraction stays identical to Brave / Ollama — the agent does its
own synthesis from snippets. Flipping the flag is a follow-up.
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


_SEARCH_URL = "https://api.tavily.com/search"
_EXTRACT_URL = "https://api.tavily.com/extract"
_REQUEST_TIMEOUT = 30


def _get_api_key() -> str:
    """``tools.web_search.tavily.api_key`` (YAML) → ``TAVILY_API_KEY`` env.

    Tavily's docs document keys with a ``tvly-`` prefix — we don't enforce
    it here (the server validates), but typo-style values like a key
    without the prefix will simply 401.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        key = cfg._raw.get("tools", {}).get("web_search", {}).get("tavily", {}).get("api_key", "")
        if key:
            return str(key)
    except Exception:
        pass
    return os.environ.get("TAVILY_API_KEY", "")


def _get_tavily_options() -> dict[str, Any]:
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return dict(cfg._raw.get("tools", {}).get("web_search", {}).get("tavily", {}) or {})
    except Exception:
        return {}


def _post(url: str, body: dict[str, Any], api_key: str) -> dict[str, Any]:
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
        raise ProviderError(f"tavily network error: {exc}") from exc

    # Tavily-specific status codes (per docs):
    #   429 — rate limit
    #   432 — plan limit reached
    #   433 — pay-as-you-go cap reached
    # All three are quota-style failures; map to RateLimitError so the
    # fallback chain treats them identically.
    if resp.status_code in (429, 432, 433):
        raise RateLimitError(f"tavily quota/rate limit (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 500:
        raise ProviderError(f"tavily server error (HTTP {resp.status_code}): {resp.text[:200]}")
    if resp.status_code >= 400:
        raise ProviderError(f"tavily API error (HTTP {resp.status_code}): {resp.text[:200]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise ProviderError(f"tavily returned non-JSON: {resp.text[:200]}") from exc


class TavilySearchProvider:
    name = "tavily"

    def is_available(self) -> bool:
        return bool(_get_api_key())

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        api_key = _get_api_key()
        if not api_key:
            raise ProviderNotConfigured(
                "tavily search: no API key (set TAVILY_API_KEY or tools.web_search.tavily.api_key in config.yaml)"
            )

        opts = _get_tavily_options()
        # Tavily's max_results is 0..20 per docs; defensive clamp.
        n = max(1, min(int(max_results), 20))
        body: dict[str, Any] = {
            "query": query,
            "max_results": n,
            "search_depth": opts.get("search_depth") or "basic",
            "topic": opts.get("topic") or "general",
            # Explicitly off — see module docstring.
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
        }
        # Optional refinements — only include when the user set them.
        for key in ("country", "time_range", "start_date", "end_date"):
            val = opts.get(key)
            if val:
                body[key] = val

        data = _post(_SEARCH_URL, body, api_key)
        raw_results = data.get("results") or []

        out: list[SearchResult] = []
        for r in raw_results:
            score = r.get("score")
            try:
                score_f = float(score) if score is not None else None
            except (TypeError, ValueError):
                score_f = None
            out.append(
                SearchResult(
                    title=str(r.get("title") or "(no title)"),
                    url=str(r.get("url") or ""),
                    content=str(r.get("content") or "").strip(),
                    score=score_f,
                )
            )
        return out


class TavilyFetchProvider:
    name = "tavily"

    def is_available(self) -> bool:
        return bool(_get_api_key())

    def fetch(self, url: str) -> FetchResult:
        api_key = _get_api_key()
        if not api_key:
            raise ProviderNotConfigured(
                "tavily fetch: no API key (set TAVILY_API_KEY or tools.web_search.tavily.api_key in config.yaml)"
            )

        body = {
            "urls": [url],
            "extract_depth": "basic",
            "format": "markdown",
        }
        data = _post(_EXTRACT_URL, body, api_key)

        # Tavily's /extract returns ``results: [{url, raw_content, ...}]``.
        # We only requested one URL so take the first hit; if the API
        # returned a ``failed_results`` entry instead, treat it as an
        # error so the fallback chain can advance.
        results = data.get("results") or []
        if not results:
            failed = data.get("failed_results") or []
            if failed:
                reason = failed[0].get("error") or "extract failed"
                raise ProviderError(f"tavily extract failed: {reason}")
            raise ProviderError("tavily extract returned no results")

        first = results[0]
        content = str(first.get("raw_content") or first.get("content") or "")
        # Tavily doesn't return a separate title — try to derive one from
        # the first markdown H1, fall back to "" so the formatter renders
        # the URL as the heading.
        title = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("# "):
                title = stripped[2:].strip()
                break

        return FetchResult(
            title=title,
            url=str(first.get("url") or url),
            content=content,
            links=[],
        )


__all__ = ["TavilySearchProvider", "TavilyFetchProvider"]
