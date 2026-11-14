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
        provider: tavily                       # one of ollama|brave|tavily
        provider_fallbacks: [ollama]           # ordered list of providers to try on 429/5xx
        tavily:
          enabled: true                        # false drops it from search AND fetch chains
          api_key: ""
          max_results: 5
          fetch_max_chars: 8000
        ollama: {...}
      web_fetch:
        provider: ollama                       # one of ollama|tavily (brave has no fetch)
        provider_fallbacks: []                 # not a retry list; see module docstring

Per-provider settings are read via ``settings.py``.

Brave is NOT registered in the fetch table — only ``ollama`` and
``tavily`` can serve fetches. Passing ``brave`` as a fetch provider
raises ``ProviderError`` at chain-resolution time so the misconfig is
caught loudly.
"""

from __future__ import annotations

import threading
import time

from chalkbox.logging.bridge import get_logger

from .base import (
    FetchFailed,
    FetchProvider,
    FetchResult,
    NoProviderAvailable,
    ProviderError,
    ProviderNotConfigured,
    RateLimitError,
    SearchProvider,
    SearchResult,
)
from .brave import BraveSearchProvider
from .cooldown import ProviderCooldown, cooldown_ttl, cooldown_until, get_provider_cooldown
from .ollama import OllamaFetchProvider, OllamaSearchProvider
from .settings import is_enabled, max_results_for
from .tavily import TavilyFetchProvider, TavilySearchProvider

logger = get_logger(__name__)

# One research job is capped at 900s. Hold a failed URL for that long so
# a sub-agent (or the same one) cannot fetch it again during the run.
_FETCH_FAILURE_TTL_S = 900
_fetch_failures: dict[str, tuple[float, FetchFailed]] = {}
_fetch_failure_lock = threading.Lock()


def clear_fetch_failures() -> None:
    """Drop the failed-URL cache. Tests use this."""
    with _fetch_failure_lock:
        _fetch_failures.clear()


def _lookup_fetch_failure(url: str) -> FetchFailed | None:
    now = time.monotonic()
    with _fetch_failure_lock:
        hit = _fetch_failures.get(url)
        if hit is None:
            return None
        expires, exc = hit
        if expires <= now:
            _fetch_failures.pop(url, None)
            return None
        return exc


def _store_fetch_failure(exc: FetchFailed) -> None:
    with _fetch_failure_lock:
        _fetch_failures[exc.url] = (time.monotonic() + _FETCH_FAILURE_TTL_S, exc)


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
    """Look up each name in ``registry``; drop unknown / duplicate / disabled."""
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
        if not is_enabled(name):
            logger.info("[search_providers] %s provider %r disabled — skipping", kind, name)
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


_DISPLAY_NAMES = {"ollama": "Ollama Cloud", "tavily": "Tavily", "brave": "Brave"}


def _skip_reason(provider, cooldown: ProviderCooldown) -> str | None:
    """Why ``provider`` gets skipped without a request, or ``None`` to try it.

    Key check is cheap and comes first so the log reads "no API key"
    rather than a 401 from the vendor.
    """
    if not provider.is_available():
        return "no API key"
    remaining = cooldown.remaining(provider.name)
    if remaining is not None:
        return f"rate limited until {cooldown_until(remaining)}"
    return None


def describe_search_route() -> str:
    """Which provider the next search will go to, for the @search routing card.

    Uses the same skip rules as ``_walk`` but makes no request, so a
    provider failing mid-run isn't reflected until the next run.
    """
    cooldown = get_provider_cooldown()
    reasons: dict[str, str] = {}
    for provider in _resolve_search_chain():
        skip = _skip_reason(provider, cooldown)
        if skip is not None:
            reasons[provider.name] = skip
            continue
        label = f"web search via {_DISPLAY_NAMES.get(provider.name, provider.name)}"
        if reasons:
            label += " (fallback — " + "; ".join(f"{n}: {why}" for n, why in reasons.items()) + ")"
        return label
    detail = "; ".join(f"{n}: {why}" for n, why in reasons.items()) or "no enabled provider configured"
    return f"no web search provider available ({detail})"


def _walk(
    chain: list,
    op_name: str,
    op_callable,
    *,
    stop_on_error: bool = False,
):
    """Generic fallback walk. ``op_callable`` is a closure that takes a
    provider and returns the result; advances on ``ProviderError`` /
    ``RateLimitError`` / ``ProviderNotConfigured``; other exceptions
    bubble up unchanged.

    ``stop_on_error`` (fetch) re-raises the first ``ProviderError`` or
    ``RateLimitError`` instead of calling the next provider.
    ``ProviderNotConfigured`` still advances: that provider never tried
    the URL. Providers in cooldown are skipped without a request. A rate
    limit starts a cooldown (see ``cooldown.py``). When nothing is left,
    raises ``NoProviderAvailable`` with a reason per provider.
    """
    cooldown = get_provider_cooldown()
    reasons: dict[str, str] = {}
    for idx, provider in enumerate(chain):
        skip = _skip_reason(provider, cooldown)
        if skip is not None:
            logger.info(
                "[%s] provider %r skipped: %s (%d/%d)",
                op_name,
                provider.name,
                skip,
                idx + 1,
                len(chain),
            )
            reasons[provider.name] = skip
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
            reasons[provider.name] = "no API key"
        except RateLimitError as exc:
            ttl = cooldown_ttl(exc)
            cooldown.mark(provider.name, ttl, f"HTTP {exc.status_code or '?'}")
            reasons[provider.name] = f"rate limited until {cooldown_until(ttl)}"
            logger.warning(
                "[%s] provider %r rate limited: %s%s",
                op_name,
                provider.name,
                exc,
                "" if stop_on_error else " — falling back",
            )
            if stop_on_error:
                raise
        except ProviderError as exc:
            logger.warning(
                "[%s] provider %r failed: %s%s",
                op_name,
                provider.name,
                exc,
                "" if stop_on_error else " — falling back",
            )
            if stop_on_error:
                raise
            reasons[provider.name] = str(exc)[:200]
    raise NoProviderAvailable(op_name, reasons)


def search_with_fallback(query: str, max_results: int = 0) -> list[SearchResult]:
    """``max_results <= 0`` means each provider uses its own configured value."""
    chain = _resolve_search_chain()
    return _walk(
        chain,
        "search",
        lambda p: p.search(query, max_results if max_results > 0 else max_results_for(p.name)),
    )


def fetch_with_fallback(url: str, query: str = "") -> FetchResult:
    """Fetch ``url`` once. The result's ``provider`` is who served it.

    ``query`` is forwarded to the provider. Tavily uses it to return the
    relevant chunks; an empty query fetches the whole page.

    The first ``ProviderError`` or ``RateLimitError`` stops the chain.
    The same URL raises :class:`FetchFailed` from cache until the TTL
    passes, with no further HTTP call.
    """
    cached = _lookup_fetch_failure(url)
    if cached is not None:
        logger.info("[fetch] %s already failed — not retrying", url[:120])
        raise cached

    def _fetch(provider) -> FetchResult:
        try:
            result = provider.fetch(url, query=query)
        except RateLimitError:
            raise
        except ProviderNotConfigured:
            raise
        except ProviderError as exc:
            raise FetchFailed(url, str(exc), provider=provider.name) from exc
        result.provider = provider.name
        return result

    try:
        return _walk(_resolve_fetch_chain(), "fetch", _fetch, stop_on_error=True)
    except FetchFailed as exc:
        _store_fetch_failure(exc)
        raise
    except RateLimitError as exc:
        failed = FetchFailed(url, str(exc))
        _store_fetch_failure(failed)
        raise failed from exc


__all__ = [
    "FetchFailed",
    "FetchResult",
    "NoProviderAvailable",
    "ProviderError",
    "RateLimitError",
    "SearchResult",
    "describe_search_route",
    "fetch_with_fallback",
    "get_fetch_provider",
    "get_search_provider",
    "search_with_fallback",
]
