"""Session-scoped Redis cache for agent-gathered data.

When the agent fetches a website, reads a file, or calls an API, the result
data is available for follow-up queries within the same session without
re-fetching. This store is keyed by (session_id, label) — the LLM can
reference cached data by semantic label ("the data from earlier", "the
config file I read", etc.) instead of re-requesting the same information.

Unlike tool_cache (which is tool+args keyed), this is session+label keyed
so multi-step agent workflows can reference intermediate results by name.

Usage:
    from web.server.result_store import result_store

    # Store a result from a tool execution
    result_store.store(
        session_id="user-123",
        label="config_yaml",
        data=config_text,
        tool_name="read_file",
        source_url="/app/config.yaml",
        content_type="yaml"
    )

    # Retrieve in a follow-up query
    result = result_store.get(session_id="user-123", label="config_yaml")
    if result:
        print(f"Found {result['content_type']} from {result['tool_name']}")
        data = result['data']

    # List available data in a session (for LLM context injection)
    summary = result_store.get_summary(session_id="user-123")
    # → [
    #     {"label": "config_yaml", "tool_name": "read_file", "content_type": "yaml", "size": 2048, "age": "2m"},
    #     {"label": "web_fetch", "tool_name": "web_fetch", "content_type": "html", "size": 15000, "age": "5m"},
    #   ]
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from app.config import settings as _af_settings

logger = logging.getLogger(__name__)

_DEFAULT_SESSION_TTL = 1800  # 30 minutes
_DEFAULT_MAX_ENTRY_SIZE = 100_000  # 100KB per entry
_DEFAULT_LABEL_TRUNCATE = _af_settings.memory.result_store_label_truncate  # max label length

# Tools that should auto-store their results when called
AUTO_STORE_TOOLS = frozenset(
    {
        "web_search",
        "web_fetch",
        "web_scrape",
        "read_file",
        "write_file",
        "shell",
        "dns_lookup",
        "whois_lookup",
        "get_headers",
        "tmdb_search",
        "tmdb_movie_details",
        "tmdb_tv_details",
        "check_availability",
        "sql_query",
    }
)


def auto_label(tool_name: str, args: dict) -> str:
    """Generate a human-readable label from tool name and key arguments.

    Examples:
        web_search(query="python asyncio") → "web_search: python asyncio"
        read_file(path="/app/config.yaml") → "file: config.yaml"
        web_fetch(url="https://example.com/api") → "fetch: example.com/api"
        shell(command="docker ps") → "shell: docker ps"
        dns_lookup(domain="example.com") → "dns: example.com"
    """
    # Prefer the most semantically useful arg for each tool
    key_arg = None

    if tool_name in ("web_search",):
        key_arg = args.get("query", args.get("q", ""))
    elif tool_name in ("web_fetch", "web_scrape"):
        url = args.get("url", "")
        if url:
            # Extract domain + path (truncate long URLs)
            try:
                from urllib.parse import urlparse

                parsed = urlparse(url)
                key_arg = parsed.netloc + (parsed.path[:30] if parsed.path else "")
            except Exception:
                key_arg = url[:40]
    elif tool_name == "read_file":
        key_arg = args.get("path", args.get("file_path", ""))
        if key_arg:
            # Just the filename
            key_arg = key_arg.split("/")[-1]
    elif tool_name == "write_file":
        key_arg = args.get("path", args.get("file_path", ""))
        if key_arg:
            key_arg = key_arg.split("/")[-1]
    elif tool_name == "shell":
        key_arg = args.get("command", "")
        if key_arg:
            # Just the first word of the command
            key_arg = key_arg.split()[0] if key_arg else ""
    elif tool_name in ("dns_lookup", "whois_lookup"):
        key_arg = args.get("domain", "")
    elif tool_name == "get_headers":
        key_arg = args.get("url", "")
        if key_arg:
            try:
                from urllib.parse import urlparse

                key_arg = urlparse(key_arg).netloc
            except Exception:
                pass
    elif tool_name in ("tmdb_search", "tmdb_movie_details", "tmdb_tv_details"):
        key_arg = args.get("query", args.get("name", ""))
    elif tool_name == "check_availability":
        key_arg = args.get("url", "")
    elif tool_name == "sql_query":
        key_arg = args.get("query", "")
        if key_arg:
            # First ~20 chars of the SQL
            key_arg = key_arg[:20].strip()

    # Build the label
    if key_arg:
        # Truncate long arg values
        key_arg = str(key_arg)[:_DEFAULT_LABEL_TRUNCATE].strip()
        if not key_arg.endswith("...") and len(key_arg) == _DEFAULT_LABEL_TRUNCATE:
            key_arg += "..."
        return f"{tool_name}: {key_arg}"
    else:
        return tool_name


class ResultStore:
    """Redis-backed session-scoped result store for agent data.

    Stores large intermediate results from tool executions under semantic
    labels so follow-up queries can reference them without re-fetching.

    Key pattern: `resultstore:{session_id}:{label}`
    Index pattern: `resultstore:{session_id}:_index` (Redis SET of label names)

    Each stored result is a Redis HASH with fields:
        data: the actual content (truncated to max_entry_size)
        tool_name: which tool produced it
        content_type: "text", "json", "html", "csv", "yaml", etc.
        source_url: where it came from (URL, file path, etc.)
        stored_at: ISO 8601 timestamp (UTC)
        size: byte length of original data (before truncation)
    """

    def __init__(
        self,
        redis_url: str | None = None,
        session_ttl: int = _DEFAULT_SESSION_TTL,
        max_entry_size: int = _DEFAULT_MAX_ENTRY_SIZE,
    ) -> None:
        """Initialize the result store."""
        self._redis = None
        self._url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379")
        self._session_ttl = session_ttl
        self._max_entry_size = max_entry_size
        self._ready = False
        self._connect()

    def _connect(self) -> None:
        """Establish Redis connection. Logs warning but continues if unavailable."""
        try:
            import redis

            self._redis = redis.from_url(self._url, decode_responses=True)
            self._redis.ping()
            self._ready = True
            logger.info(
                "ResultStore connected to Redis at %s (session_ttl=%ds)",
                self._url,
                self._session_ttl,
            )
        except Exception as exc:
            logger.warning("ResultStore Redis connection failed: %s — result caching disabled", exc)
            self._ready = False

    @staticmethod
    def _make_key(session_id: str, label: str) -> str:
        """Deterministic Redis key from session + label."""
        # Normalize label: lowercase, replace spaces with underscores, max 100 chars
        norm_label = label.lower().replace(" ", "_")[:100]
        return f"resultstore:{session_id}:{norm_label}"

    @staticmethod
    def _make_index_key(session_id: str) -> str:
        """Redis SET key for all labels in a session."""
        return f"resultstore:{session_id}:_index"

    def store(
        self,
        session_id: str,
        label: str,
        data: str,
        *,
        tool_name: str = "",
        content_type: str = "text",
        source_url: str = "",
        ttl: int | None = None,
    ) -> bool:
        """Store a result under a human-readable label for the session."""
        if not self._ready:
            return False

        if len(data) > self._max_entry_size:
            logger.debug(
                "ResultStore SKIP (too large): %s:%s (%d bytes)",
                session_id,
                label,
                len(data),
            )
            return False

        try:
            key = self._make_key(session_id, label)
            index_key = self._make_index_key(session_id)
            ttl = ttl or self._session_ttl

            # Store the data as a HASH with metadata
            now_iso = datetime.now(timezone.utc).isoformat()
            self._redis.hset(
                key,
                mapping={
                    "data": data,
                    "tool_name": tool_name,
                    "content_type": content_type,
                    "source_url": source_url,
                    "stored_at": now_iso,
                    "size": str(len(data)),
                },
            )

            # Set TTL on the result
            self._redis.expire(key, ttl)

            # Add label to the session's index (also with TTL)
            self._redis.sadd(index_key, label)
            self._redis.expire(index_key, ttl)

            logger.debug(
                "ResultStore SET: %s:%s (tool=%s, type=%s, ttl=%ds)",
                session_id,
                label,
                tool_name,
                content_type,
                ttl,
            )
            return True

        except Exception as exc:
            logger.debug("ResultStore SET failed: %s", exc)
            return False

    def get(self, session_id: str, label: str) -> dict | None:
        """Retrieve a stored result."""
        if not self._ready:
            return None

        try:
            key = self._make_key(session_id, label)
            result = self._redis.hgetall(key)
            if result:
                logger.debug("ResultStore GET HIT: %s:%s", session_id, label)
                return result
            return None
        except Exception:
            return None

    def list_labels(self, session_id: str) -> list[str]:
        """Return all stored labels for a session."""
        if not self._ready:
            return []

        try:
            index_key = self._make_index_key(session_id)
            labels = list(self._redis.smembers(index_key) or [])
            return sorted(labels)
        except Exception:
            return []

    def get_summary(self, session_id: str) -> list[dict]:
        """Return metadata for all stored results in a session.

        Does NOT include the data field — just metadata for the LLM to
        reference and decide what to use.
        """
        if not self._ready:
            return []

        try:
            labels = self.list_labels(session_id)
            summary = []
            now = datetime.now(timezone.utc)

            for label in labels:
                key = self._make_key(session_id, label)
                result = self._redis.hgetall(key)
                if not result:
                    continue

                # Calculate age
                stored_at_str = result.get("stored_at", "")
                age_str = "unknown"
                if stored_at_str:
                    try:
                        stored_at = datetime.fromisoformat(stored_at_str)
                        delta = now - stored_at
                        age_str = self._format_timedelta(delta.total_seconds())
                    except Exception:
                        pass

                summary.append(
                    {
                        "label": label,
                        "tool_name": result.get("tool_name", ""),
                        "content_type": result.get("content_type", "text"),
                        "source_url": result.get("source_url", ""),
                        "size": int(result.get("size", 0)),
                        "age": age_str,
                    }
                )

            return sorted(summary, key=lambda x: x["label"])

        except Exception:
            return []

    def delete(self, session_id: str, label: str) -> bool:
        """Remove a specific result from the store."""
        if not self._ready:
            return False

        try:
            key = self._make_key(session_id, label)
            index_key = self._make_index_key(session_id)

            # Delete the result
            deleted = self._redis.delete(key)

            # Remove from index
            self._redis.srem(index_key, label)

            if deleted:
                logger.debug("ResultStore DELETE: %s:%s", session_id, label)
            return bool(deleted)

        except Exception:
            return False

    def clear_session(self, session_id: str) -> int:
        """Remove all stored results for a session."""
        if not self._ready:
            return 0

        try:
            labels = self.list_labels(session_id)
            deleted_count = 0

            for label in labels:
                key = self._make_key(session_id, label)
                deleted_count += self._redis.delete(key)

            # Delete the index
            index_key = self._make_index_key(session_id)
            deleted_count += self._redis.delete(index_key)

            if deleted_count:
                logger.debug("ResultStore CLEAR: %s (deleted %d items)", session_id, deleted_count)

            return deleted_count

        except Exception:
            return 0

    def stats(self) -> dict:
        """Return store statistics."""
        if not self._ready:
            return {"status": "not_ready"}

        try:
            # Count result keys (excluding index keys)
            result_keys = self._redis.keys("resultstore:*:*") or []
            # Filter out _index keys
            result_keys = [k for k in result_keys if not k.endswith(":_index")]

            # Count unique sessions
            sessions = set()
            for key in result_keys:
                # Key format: resultstore:<session_id>:<label>
                parts = key.split(":", 2)
                if len(parts) >= 2:
                    sessions.add(parts[1])

            # Get memory usage
            info = self._redis.info("memory")
            memory_bytes = info.get("used_memory", 0)

            return {
                "status": "ready",
                "sessions": len(sessions),
                "total_results": len(result_keys),
                "memory_bytes": memory_bytes,
            }

        except Exception:
            return {"status": "error"}

    @staticmethod
    def _format_timedelta(seconds: float) -> str:
        """Format seconds into human-readable age string.

        Examples:
            12.5 → "12s"
            125 → "2m 5s"
            3700 → "1h 1m"
        """
        if seconds < 60:
            return f"{int(round(seconds))}s"
        if seconds < 3600:
            m = int(seconds // 60)
            s = int(round(seconds % 60))
            return f"{m}m {s}s" if s > 0 else f"{m}m"
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m > 0 else f"{h}h"


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: ResultStore | None = None


def get_result_store() -> ResultStore | None:
    """Get the shared result store instance, or None if not initialized."""
    return _instance


def init_result_store(
    redis_url: str | None = None,
    session_ttl: int = _DEFAULT_SESSION_TTL,
    max_entry_size: int = _DEFAULT_MAX_ENTRY_SIZE,
) -> ResultStore:
    """Initialize the shared result store singleton."""
    global _instance
    _instance = ResultStore(
        redis_url=redis_url,
        session_ttl=session_ttl,
        max_entry_size=max_entry_size,
    )
    return _instance
