"""Provider interfaces for web search + web fetch.

Provider classes wrap a concrete vendor API (Ollama Cloud, Brave Search,
Tavily). Above them sits ``agentforge.tools.web_search`` which exposes
``web_search`` and ``web_fetch`` as @tool-registered functions — the
agent and runners call those, and the provider plumbing below decides
which vendor actually serves the request.

The fallback chain (see ``__init__.py``) walks providers in order and
catches :class:`RateLimitError` and :class:`ProviderError` only. Network
failures, auth failures, and other transient errors are wrapped in
``ProviderError`` so they trigger the next provider; uncategorised
exceptions bubble up unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class SearchResult:
    """A single search hit, normalised across providers.

    - ``title`` / ``url`` / ``content`` are required, always populated.
    - ``score`` is Tavily-specific (relevance 0..1).
    - ``age`` is Brave-specific (freshness string like "3 days ago").
    """

    title: str
    url: str
    content: str
    score: float | None = None
    age: str | None = None


@dataclass
class FetchResult:
    """A single page fetch, normalised across providers.

    ``links`` may be empty when the provider doesn't surface them
    (Tavily's /extract returns markdown content but no link list).
    """

    title: str
    url: str
    content: str
    links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ProviderError(RuntimeError):
    """Generic provider failure (4xx other than 429, 5xx, network, parse).

    Caught by the fallback chain — next provider in line takes over.
    """


class RateLimitError(ProviderError):
    """Explicit rate-limit / quota-exhaustion signal from a provider.

    Separated from generic ProviderError so callers can log "X is rate
    limited" specifically when deciding whether to bother trying the
    same provider again later.
    """


class ProviderNotConfigured(ProviderError):
    """Raised when a provider lacks its API key — never made an HTTP call.

    Treated by the fallback chain identically to other ProviderErrors
    (advance to the next), but lets the registry skip the provider
    upfront without trying it at all.
    """


# ---------------------------------------------------------------------------
# Protocols
# ---------------------------------------------------------------------------


@runtime_checkable
class SearchProvider(Protocol):
    """Search-vendor abstraction. Concrete impls live next to this file."""

    name: str  # short identifier — "ollama" | "brave" | "tavily"

    def is_available(self) -> bool:
        """Cheap check: is the provider configured? Should not make a
        network request — used by the registry to skip the provider
        before the first call when the API key is missing.
        """
        ...

    def search(self, query: str, max_results: int) -> list[SearchResult]:
        """Run the search. Raise ``RateLimitError`` on 429-equivalent,
        ``ProviderError`` on other failures. Return at most
        ``max_results`` items.
        """
        ...


@runtime_checkable
class FetchProvider(Protocol):
    """Page-fetch vendor abstraction. Same shape as SearchProvider."""

    name: str

    def is_available(self) -> bool: ...

    def fetch(self, url: str) -> FetchResult:
        """Fetch ``url``. Raise ``RateLimitError`` / ``ProviderError``
        on failure. Returns a normalised :class:`FetchResult`.
        """
        ...


__all__ = [
    "SearchResult",
    "FetchResult",
    "SearchProvider",
    "FetchProvider",
    "ProviderError",
    "RateLimitError",
    "ProviderNotConfigured",
]
