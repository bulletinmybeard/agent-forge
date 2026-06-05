"""Web search and web fetch tools — vendor-agnostic.

Provides ``web_search`` and ``web_fetch`` tool functions backed by a
pluggable provider abstraction (see :mod:`agentforge.tools.search_providers`).

Default provider is Ollama Cloud (unchanged behaviour from the original
single-vendor implementation). Operators can switch to Brave or Tavily
via ``tools.web_search.provider`` / ``tools.web_fetch.provider`` in
``config.yaml`` and configure cross-provider fallbacks for resilience
against rate limits / outages.

Requirements:
  - At least one provider with a valid API key. Today:
      • Ollama Cloud — set ``OLLAMA_API_KEY`` env or ``tools.web_search.api_key``
      • Brave Search — set ``BRAVE_API_KEY`` env or ``tools.web_search.brave.api_key``
      • Tavily Search — set ``TAVILY_API_KEY`` env or ``tools.web_search.tavily.api_key``

The tools gracefully disable themselves when no provider is configured —
they return an informative error instead of crashing.

Usage::

    from agentforge.tools import ToolRegistry
    from agentforge.tools.web_search import register_web_search_tools

    registry = ToolRegistry()
    register_web_search_tools(registry)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from chalkbox.logging.bridge import get_logger

from .registry import tool
from .search_providers import (
    FetchResult,
    ProviderError,
    SearchResult,
    fetch_with_fallback,
    search_with_fallback,
)
from .search_providers import _resolve_fetch_chain as _resolve_fetch_chain  # noqa: F401
from .search_providers import _resolve_search_chain as _resolve_search_chain  # noqa: F401

if TYPE_CHECKING:
    from .registry import ToolRegistry

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_max_results() -> int:
    """Max search results to return (default: 5, hard cap: 20).

    The hard cap matches Brave + Tavily limits; Ollama's API max was 10
    historically but accepts larger values silently. We clamp to 20 so
    the cap is consistent across providers.
    """
    try:
        from agentforge.config import get_config

        cfg = get_config()
        n = cfg._raw.get("tools", {}).get("web_search", {}).get("max_results", 5)
        return min(int(n), 20)
    except Exception:
        return 5


def _get_fetch_max_chars() -> int:
    """Max characters to return from web_fetch (default: 8000)."""
    try:
        from agentforge.config import get_config

        cfg = get_config()
        return int(cfg._raw.get("tools", {}).get("web_search", {}).get("fetch_max_chars", 8000))
    except Exception:
        return 8000


# ---------------------------------------------------------------------------
# Availability — back-compat shim for callers that pre-check.
# ---------------------------------------------------------------------------


def is_web_search_available() -> bool:
    """Return True if at least one configured search provider has an API key.

    The original implementation probed Ollama with a live HTTP call;
    the provider-aware replacement is a cheap key-presence check across
    every provider in the search chain. Saves the startup probe and
    works correctly when the operator only configured Brave / Tavily.
    """
    try:
        chain = _resolve_search_chain()
    except Exception:
        return False
    return any(p.is_available() for p in chain)


def is_web_fetch_available() -> bool:
    """Same idea as ``is_web_search_available`` but for the fetch chain."""
    try:
        chain = _resolve_fetch_chain()
    except Exception:
        return False
    return any(p.is_available() for p in chain)


# ---------------------------------------------------------------------------
# Formatters — vendor-agnostic, single source of truth for LLM-facing text
# ---------------------------------------------------------------------------


def _format_search_results(query: str, results: list[SearchResult]) -> str:
    """Render a list of normalised :class:`SearchResult` for the agent.

    Same shape as the original web_search output:

        Web search results for: <query>

        1. <title>
           URL: <url>
           <truncated content>

        2. ...
    """
    if not results:
        return f"No results found for: {query}"
    lines: list[str] = [f"Web search results for: {query}\n"]
    for i, r in enumerate(results, 1):
        title = r.title or "(no title)"
        content = (r.content or "").strip()
        if len(content) > 500:
            content = content[:500] + "..."
        lines.append(f"{i}. {title}")
        if r.url:
            lines.append(f"   URL: {r.url}")
        if content:
            lines.append(f"   {content}")
        lines.append("")
    return "\n".join(lines)


def _format_fetch_result(url: str, result: FetchResult, max_chars: int) -> str:
    """Render a :class:`FetchResult` for the agent — same shape as before."""
    content = result.content or ""
    if not content:
        return f"No content returned for: {url}"
    truncated = False
    if len(content) > max_chars:
        content = content[:max_chars]
        truncated = True

    parts: list[str] = []
    if result.title:
        parts.append(f"# {result.title}")
        parts.append(f"Source: {url}\n")
    else:
        parts.append(f"Source: {url}\n")

    parts.append(content)

    if truncated:
        parts.append(f"\n... (truncated at {max_chars:,} chars)")

    if result.links:
        parts.append(f"\nLinks found: {len(result.links)}")
        for link in result.links[:5]:
            parts.append(f"  • {link}")
        if len(result.links) > 5:
            parts.append(f"  ... and {len(result.links) - 5} more")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# web_search tool
# ---------------------------------------------------------------------------


@tool(
    locality="remote",
    hint=(
        "Use this tool to search the web for current information. "
        "Returns titles, URLs, and content snippets for the top results. "
        "Good for: recent events, documentation lookups, fact-checking, "
        "finding tutorials or examples, current pricing or availability. "
        "Parameters: query (string) — the search query in natural language. "
        "Example: web_search(query='nginx reverse proxy configuration for Node.js')"
    ),
)
def web_search(query: str, max_results: int = 0) -> str:
    """Search the web and return titles, URLs, and snippets.

    When to use: Current or external information not in local files or training data.
        Good for: recent events, documentation lookups, tutorials, pricing, availability.
    When NOT to use: To fetch full page content (use web_fetch after searching),
        local file searches (use find_files), if no API key is configured.

    query: the search query in natural language
    max_results: number of results to return (default: 5, max: 20)
    """
    if not query or not query.strip():
        return "Error: No search query provided."

    if isinstance(max_results, str):
        try:
            max_results = int(max_results)
        except ValueError:
            max_results = 0
    if max_results <= 0:
        max_results = _get_max_results()
    max_results = min(max_results, 20)

    logger.debug("[web_search] Searching: %s (max %d)", query[:80], max_results)

    try:
        results = search_with_fallback(query, max_results)
    except ProviderError as exc:
        return (
            f"Search error: {exc}\n\n"
            "Configure at least one provider's API key:\n"
            "  • Ollama:  set OLLAMA_API_KEY or tools.web_search.api_key\n"
            "  • Brave:   set BRAVE_API_KEY or tools.web_search.brave.api_key\n"
            "  • Tavily:  set TAVILY_API_KEY or tools.web_search.tavily.api_key"
        )

    logger.debug("[web_search] Got %d results", len(results))
    return _format_search_results(query, results)


# ---------------------------------------------------------------------------
# web_fetch tool
# ---------------------------------------------------------------------------


@tool(
    locality="remote",
    hint=(
        "Use this tool to fetch the full content of a specific web page. "
        "Returns the page content as clean text/markdown. "
        "Use AFTER web_search when you need the full content of a result, "
        "or when the user provides a specific URL to read. "
        "IMPORTANT: This tool fetches raw HTML only — it does NOT execute JavaScript. "
        "For React, Vue, Angular, Next.js, Nuxt, Gatsby, Docusaurus, or any SPA/SSR site "
        "where content is rendered by JavaScript, use web_fetch_rendered instead. "
        "If you fetch a page and get back an empty shell or minimal content, "
        "retry with web_fetch_rendered. "
        "Parameters: url (string) — the full URL to fetch. "
        "Example: web_fetch(url='https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy/')"
    ),
)
def web_fetch(url: str) -> str:
    """Fetch the full content of a web page as clean text.

    When to use: After web_search to read a specific result in full, or when
        the user provides a specific URL to read.
    When NOT to use: For search queries (use web_search first), when you only
        need snippets/summaries (web_search is faster), if no API key is configured.

    url: the full URL to fetch (must start with http:// or https://)
    """
    if not url or not url.strip():
        return "Error: No URL provided."

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"

    logger.debug("[web_fetch] Fetching: %s", url[:120])

    try:
        result = fetch_with_fallback(url)
    except ProviderError as exc:
        return (
            f"Fetch error: {exc}\n\n"
            "Configure at least one fetch provider:\n"
            "  • Ollama:  set OLLAMA_API_KEY or tools.web_search.api_key\n"
            "  • Tavily:  set TAVILY_API_KEY or tools.web_search.tavily.api_key\n"
            "(Brave does not offer a fetch endpoint.)"
        )

    rendered = _format_fetch_result(url, result, _get_fetch_max_chars())
    logger.debug("[web_fetch] %d chars from %s", len(result.content or ""), url[:60])
    return rendered


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_web_search_tools(registry: ToolRegistry) -> int:
    """Register web search tools with the given *registry*.

    The tools are always registered (so the model knows they exist), but
    they return a helpful error message if no provider is configured.
    """
    registry.register_category_hint(
        "Web Search",
        "Search the web for current information and fetch full page content. "
        "Use web_search for queries, web_fetch for reading specific URLs. "
        "Backed by Ollama Cloud (default), Brave, or Tavily depending on "
        "tools.web_search.provider / tools.web_fetch.provider in config.",
    )

    count = 0
    for _name, func in list(globals().items()):
        if callable(func) and hasattr(func, "_is_tool") and not _name.startswith("_"):
            registry.register(func, category="Web Search")
            count += 1
            logger.debug("Registered web search tool: %s", func.__name__)
    return count


__all__ = [
    "is_web_search_available",
    "is_web_fetch_available",
    "register_web_search_tools",
    "web_fetch",
    "web_search",
]
