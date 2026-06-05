"""Provider registry + fallback chain for web search and web fetch.

Public surface:

- ``get_search_provider(name)`` / ``get_fetch_provider(name)`` —
  factory lookups for a named provider.
- ``search_with_fallback(query, max_results)`` /
  ``fetch_with_fallback(url)`` — primary + fallback walk, called by
  ``agentforge.tools.web_search`` to serve the @tool functions.

Config keys (``config.yaml``)::

    tools:
      web_search:
        provider: ollama                       # one of ollama|brave|tavily
        provider_fallbacks: []                 # ordered list of providers to try on 429/5xx
      web_fetch:
        provider: ollama                       # one of ollama|tavily (brave has no fetch)
        provider_fallbacks: []

Brave is NOT registered in the fetch table — only ``ollama`` and
``tavily`` can serve fetches. Passing ``brave`` as a fetch provider
raises ``ProviderError`` at chain-resolution time so the misconfig is
caught loudly.
"""

from __future__ import annotations

from chalkbox.logging.bridge import get_logger

from .base import (
    FetchProvider,
    FetchResult,
    ProviderError,
    ProviderNotConfigured,
    RateLimitError,
    SearchProvider,
    SearchResult,
)
from .brave import BraveSearchProvider
from .ollama import OllamaFetchProvider, OllamaSearchProvider
from .tavily import TavilyFetchProvider, TavilySearchProvider

logger = get_logger(__name__)


# Provider instances are cheap (just a name + a few config lookups) —
# instantiate once at module load. The actual HTTP work happens per call.
_SEARCH_PROVIDERS: dict[str, SearchProvider] = {
    "ollama": OllamaSearchProvider(),
    "brave": BraveSearchProvider(),
    "tavily": TavilySearchProvider(),
}

_FETCH_PROVIDERS: dict[str, FetchProvider] = {
    "ollama": OllamaFetchProvider(),
    "tavily": TavilyFetchProvider(),
}


# ---------------------------------------------------------------------------
# Config readers
# ---------------------------------------------------------------------------


def _resolve_search_chain() -> list[SearchProvider]:
    """Return ``[primary, *fallbacks]`` for search.

    Unknown provider names are logged and dropped — the chain doesn't
    fail at construction so a typo in fallbacks doesn't kill search.
    The primary defaults to ``ollama`` for backward compat.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        ws_cfg = cfg._raw.get("tools", {}).get("web_search", {}) or {}
    except Exception:
        ws_cfg = {}
    return _build_chain(
        primary=str(ws_cfg.get("provider", "ollama") or "ollama"),
        fallbacks=list(ws_cfg.get("provider_fallbacks") or []),
        registry=_SEARCH_PROVIDERS,
        kind="search",
    )


def _resolve_fetch_chain() -> list[FetchProvider]:
    """Return ``[primary, *fallbacks]`` for fetch.

    Reads ``tools.web_fetch.*`` first, then falls back to
    ``tools.web_search.*`` to inherit the same provider preference
    when the operator only configured one. ``brave`` is intentionally
    not in ``_FETCH_PROVIDERS`` and gets dropped with a warning if
    listed.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        root = cfg._raw.get("tools", {})
        wf_cfg = root.get("web_fetch", {}) or {}
        ws_cfg = root.get("web_search", {}) or {}
    except Exception:
        wf_cfg, ws_cfg = {}, {}

    primary = wf_cfg.get("provider") or ws_cfg.get("provider") or "ollama"
    fallbacks = list(wf_cfg.get("provider_fallbacks") or ws_cfg.get("provider_fallbacks") or [])
    return _build_chain(
        primary=str(primary),
        fallbacks=fallbacks,
        registry=_FETCH_PROVIDERS,
        kind="fetch",
    )


def _build_chain(
    *,
    primary: str,
    fallbacks: list[str],
    registry: dict,
    kind: str,
) -> list:
    """Look up each name in ``registry``; drop unknown / duplicate."""
    chain = []
    seen: set[str] = set()
    for name in [primary, *fallbacks]:
        name = (name or "").strip().lower()
        if not name or name in seen:
            continue
        seen.add(name)
        provider = registry.get(name)
        if provider is None:
            logger.warning(
                "[search_providers] unknown %s provider %r — skipping",
                kind,
                name,
            )
            continue
        chain.append(provider)
    return chain


# ---------------------------------------------------------------------------
# Public lookups
# ---------------------------------------------------------------------------


def get_search_provider(name: str) -> SearchProvider:
    """Return a registered search provider by name. Raises KeyError."""
    try:
        return _SEARCH_PROVIDERS[name.lower()]
    except KeyError as exc:
        raise KeyError(f"unknown search provider {name!r}; known: {sorted(_SEARCH_PROVIDERS)}") from exc


def get_fetch_provider(name: str) -> FetchProvider:
    """Return a registered fetch provider by name. Raises KeyError."""
    try:
        return _FETCH_PROVIDERS[name.lower()]
    except KeyError as exc:
        raise KeyError(f"unknown fetch provider {name!r}; known: {sorted(_FETCH_PROVIDERS)}") from exc


# ---------------------------------------------------------------------------
# Fallback walkers
# ---------------------------------------------------------------------------


def _walk(
    chain: list,
    op_name: str,
    op_callable,
):
    """Generic fallback walk. ``op_callable`` is a closure that takes a
    provider and returns the result; advances on ``ProviderError`` /
    ``RateLimitError`` / ``ProviderNotConfigured``; other exceptions
    bubble up unchanged.
    """
    if not chain:
        raise ProviderError(f"no {op_name} providers configured — set tools.{op_name}.provider")
    last_exc: BaseException | None = None
    for idx, provider in enumerate(chain):
        # Cheap pre-check — skip providers without an API key entirely
        # so the warning log reads "primary=X had no key, trying Y"
        # rather than "primary=X 401 (no key)".
        if not provider.is_available():
            logger.info(
                "[%s] provider %r unavailable (no key) — skipping (%d/%d)",
                op_name,
                provider.name,
                idx + 1,
                len(chain),
            )
            continue
        try:
            return op_callable(provider)
        except ProviderNotConfigured as exc:
            # Shouldn't happen — is_available() returned True but the
            # call still raised ProviderNotConfigured. Treat as advance.
            logger.info(
                "[%s] provider %r reported not configured: %s — falling through",
                op_name,
                provider.name,
                exc,
            )
            last_exc = exc
            continue
        except RateLimitError as exc:
            logger.warning(
                "[%s] provider %r rate limited: %s — falling back",
                op_name,
                provider.name,
                exc,
            )
            last_exc = exc
            continue
        except ProviderError as exc:
            logger.warning(
                "[%s] provider %r failed: %s — falling back",
                op_name,
                provider.name,
                exc,
            )
            last_exc = exc
            continue
    # Exhausted the chain.
    if last_exc is not None:
        raise last_exc
    raise ProviderError(f"no {op_name} provider in chain has a configured API key")


def search_with_fallback(query: str, max_results: int) -> list[SearchResult]:
    chain = _resolve_search_chain()
    return _walk(chain, "search", lambda p: p.search(query, max_results))


def fetch_with_fallback(url: str) -> FetchResult:
    chain = _resolve_fetch_chain()
    return _walk(chain, "fetch", lambda p: p.fetch(url))


# ---------------------------------------------------------------------------
# Test seams
# ---------------------------------------------------------------------------


def _set_search_provider_for_test(name: str, provider) -> None:
    """Override a registered provider — tests only."""
    _SEARCH_PROVIDERS[name] = provider


def _set_fetch_provider_for_test(name: str, provider) -> None:
    _FETCH_PROVIDERS[name] = provider


__all__ = [
    "FetchResult",
    "ProviderError",
    "RateLimitError",
    "SearchResult",
    "fetch_with_fallback",
    "get_fetch_provider",
    "get_search_provider",
    "search_with_fallback",
]
