"""Tool Result Cache — Redis-backed caching for expensive tool calls.

Caches the output of tool executions (TMDB lookups, web searches, DNS, etc.)
in Redis with a configurable TTL.  Follow-up queries referencing the same data
skip the API call and return the cached result instantly.

The cache is transparent to the agent loop — it wraps tool execution at the
framework bridge level.

Usage:
    from web.server.tool_cache import tool_cache

    # Check cache before executing
    cached = tool_cache.get("web_search", {"query": "python 3.12"})
    if cached is not None:
        return cached

    result = actual_tool_call(...)
    tool_cache.set("web_search", {"query": "python 3.12"}, result)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

logger = logging.getLogger(__name__)

_DEFAULT_TTL = 300  # 5 minutes
_MAX_ENTRY_SIZE = 50_000  # skip caching results larger than 50KB

# Tools worth caching (subset of all tools)
CACHEABLE_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_scrape",
        "tmdb_search",
        "tmdb_trending",
        "tmdb_movie_details",
        "tmdb_tv_details",
        "tmdb_person_details",
        "tmdb_discover",
        "dns_lookup",
        "whois_lookup",
        "get_headers",
        "check_availability",
        "gmail_search_threads",
        "gmail_get_thread",
        "gmail_get_message",
        "gmail_list_labels",
    }
)

# Per-tool TTL overrides (seconds).  Tools not listed use _DEFAULT_TTL.
_TOOL_TTLS: dict[str, int] = {
    "check_availability": 60,  # service status changes fast
    "get_headers": 60,
    "dns_lookup": 120,
    "tmdb_search": 3600,  # media metadata is stable
    "tmdb_trending": 3600,
    "tmdb_movie_details": 3600,
    "tmdb_tv_details": 3600,
    "tmdb_person_details": 3600,
    "tmdb_discover": 3600,
    "gmail_list_labels": 3600,  # labels change rarely
    "gmail_search_threads": 300,
    "gmail_get_thread": 300,
    "gmail_get_message": 300,
}


class ToolCache:
    """Redis-backed tool result cache with per-tool TTL."""

    def __init__(self, redis_url: str | None = None, default_ttl: int = _DEFAULT_TTL) -> None:
        self._redis = None
        self._url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._default_ttl = default_ttl
        self._ready = False
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._redis = redis.from_url(self._url, decode_responses=True)
            self._redis.ping()
            self._ready = True
            logger.info("ToolCache connected to Redis at %s", self._url)
        except Exception as exc:
            logger.warning("ToolCache Redis connection failed: %s — caching disabled", exc)
            self._ready = False

    @staticmethod
    def _make_key(tool_name: str, args: dict) -> str:
        """Deterministic cache key from tool name + args."""
        # Sort args for stability, ignore None values
        clean = {k: v for k, v in sorted(args.items()) if v is not None}
        blob = f"{tool_name}:{json.dumps(clean, sort_keys=True, default=str)}"
        h = hashlib.sha256(blob.encode()).hexdigest()[:16]
        return f"toolcache:{tool_name}:{h}"

    def get(self, tool_name: str, args: dict) -> str | None:
        """Return cached result or None."""
        if not self._ready or tool_name not in CACHEABLE_TOOLS:
            return None
        try:
            key = self._make_key(tool_name, args)
            val = self._redis.get(key)
            if val is not None:
                logger.debug("Tool cache HIT: %s (%s)", tool_name, key)
            return val
        except Exception:
            return None

    def set(self, tool_name: str, args: dict, result: str, ttl: int | None = None) -> None:
        """Cache a tool result with TTL."""
        if not self._ready or tool_name not in CACHEABLE_TOOLS:
            return
        if len(result) > _MAX_ENTRY_SIZE:
            logger.debug("Tool cache SKIP (too large): %s (%d chars)", tool_name, len(result))
            return
        try:
            key = self._make_key(tool_name, args)
            effective_ttl = ttl or _TOOL_TTLS.get(tool_name, self._default_ttl)
            self._redis.setex(key, effective_ttl, result)
            logger.debug("Tool cache SET: %s (%s, ttl=%ds)", tool_name, key, effective_ttl)
        except Exception as exc:
            logger.debug("Tool cache SET failed: %s", exc)

    def stats(self) -> dict:
        """Basic cache stats."""
        if not self._ready:
            return {"status": "not_ready"}
        try:
            info = self._redis.info("keyspace")
            keys = self._redis.keys("toolcache:*")
            return {"status": "ready", "cached_tools": len(keys), "keyspace": info}
        except Exception:
            return {"status": "error"}


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: ToolCache | None = None


def get_tool_cache() -> ToolCache | None:
    return _instance


def init_tool_cache(redis_url: str | None = None, default_ttl: int = _DEFAULT_TTL) -> ToolCache:
    global _instance
    _instance = ToolCache(redis_url=redis_url, default_ttl=default_ttl)
    return _instance
