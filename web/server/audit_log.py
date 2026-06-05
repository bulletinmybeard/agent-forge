"""Redis Streams-based audit log for AgentForge.

Records all tool executions and agent run lifecycle events for compliance,
debugging, and performance analysis. Uses Redis Streams for durability and
automatic trimming.

**Stream keys:**
- `audit:tool_executions` — Individual tool call records
- `audit:agent_runs` — Agent run lifecycle (start/complete/error/cancelled)

**Usage:**

    from web.server.audit_log import get_audit_log

    audit = get_audit_log()

    # Log a tool execution
    await audit.log_tool_execution(
        session_id="sess-123",
        tool_name="web_search",
        args={"query": "python 3.12"},
        result="<result text>",
        status="success",
        duration_ms=1234,
        mode="agent",
        model="claude-3.5-sonnet",
    )

    # Log agent run events
    await audit.log_agent_run(
        session_id="sess-123",
        event="start",
        query_preview="How do I...",
        mode="agent",
        model="claude-3.5-sonnet",
        profile="default",
    )

    await audit.log_agent_run(
        session_id="sess-123",
        event="complete",
        iterations=5,
        tool_count=3,
        tools_used="web_search,parse_url,summarize",
        total_duration_ms=5000,
    )

    # Query audit logs
    tool_calls = await audit.query_tool_executions(
        session_id="sess-123",
        tool_name="web_search",
        count=50,
    )

    stats = await audit.stats(since_ms=3600000)  # last hour
    print(f"Total tool calls: {stats['total_tool_calls']}")
    print(f"Top tools: {stats['top_tools']}")
    print(f"Error rate: {stats['error_rate']:.1%}")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"
_DEFAULT_MAX_ENTRIES = 50000
_TRIM_THRESHOLD = 50001  # trigger auto-trim when exceeded


class AuditLog:
    """Redis Streams-based audit log with sync + async API."""

    def __init__(
        self,
        redis_url: str | None = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        """Initialize audit log."""
        self._redis_url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._max_entries = max_entries
        self._trim_buffer = 1000
        self._redis_async = None
        self._redis_sync = None
        self._ready = False
        self._lock = asyncio.Lock()
        self._connect()

    def _connect(self) -> None:
        """Synchronously connect to Redis (called from __init__)."""
        try:
            import redis

            self._redis_sync = redis.from_url(self._redis_url, decode_responses=True)
            self._redis_sync.ping()
            self._ready = True
            logger.info("AuditLog connected to Redis at %s", self._redis_url)
        except Exception as exc:
            logger.warning("AuditLog Redis connection failed: %s — auditing disabled", exc)
            self._ready = False

    async def _get_async_client(self) -> Any:
        """Lazily initialize async Redis client."""
        if self._redis_async is None and self._ready:
            try:
                import redis.asyncio

                self._redis_async = redis.asyncio.from_url(self._redis_url, decode_responses=True)
                await self._redis_async.ping()
                logger.debug("AuditLog async client initialized")
            except Exception as exc:
                logger.warning("AuditLog async client failed: %s", exc)
                self._redis_async = None
        return self._redis_async

    @staticmethod
    def _now_iso() -> str:
        """Current timestamp as ISO 8601 string."""
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _truncate(text: str | None, max_chars: int) -> str:
        """Truncate text to max chars, or return empty string if None."""
        if text is None:
            return ""
        return text[: max_chars - 1] if len(text) > max_chars else text

    async def log_tool_execution(
        self,
        session_id: str,
        tool_name: str,
        args: dict[str, Any] | None = None,
        result: str | None = None,
        status: str = "success",
        error_message: str | None = None,
        duration_ms: int = 0,
        mode: str = "",
        model: str = "",
    ) -> str | None:
        """Log a tool execution to audit:tool_executions stream."""
        if not self._ready:
            return None

        # Serialize args to JSON, truncate to 2KB
        args_json = ""
        if args:
            try:
                args_json = json.dumps(args, default=str)
                args_json = self._truncate(args_json, 2048)
            except Exception as exc:
                logger.debug("Failed to serialize tool args: %s", exc)
                args_json = ""

        # Result preview (first 500 chars)
        result_preview = self._truncate(result, 500)
        result_size = len(result or "")

        # Error message truncated
        error_msg = self._truncate(error_message, 500)

        entry = {
            "session_id": session_id,
            "tool_name": tool_name,
            "args_json": args_json,
            "result_size": result_size,
            "result_preview": result_preview,
            "status": status,
            "error_message": error_msg,
            "duration_ms": duration_ms,
            "timestamp": self._now_iso(),
            "mode": mode,
            "model": model,
        }

        try:
            client = await self._get_async_client()
            if client is None:
                return None
            entry_id = await client.xadd("audit:tool_executions", entry)
            logger.debug("AuditLog tool execution: %s (%s)", tool_name, entry_id)

            # Auto-trim if needed
            await self._maybe_trim_async(client)
            return entry_id
        except Exception as exc:
            logger.warning("AuditLog tool execution failed: %s", exc)
            return None

    async def log_agent_run(
        self,
        session_id: str,
        event: str,
        query_preview: str | None = None,
        mode: str = "",
        model: str = "",
        profile: str = "",
        iterations: int = 0,
        tool_count: int = 0,
        tools_used: str = "",
        total_duration_ms: int = 0,
        error_message: str | None = None,
    ) -> str | None:
        """Log an agent run lifecycle event to audit:agent_runs stream."""
        if not self._ready:
            return None

        query_prev = self._truncate(query_preview, 200)
        error_msg = self._truncate(error_message, 500)

        entry = {
            "session_id": session_id,
            "event": event,
            "timestamp": self._now_iso(),
            "mode": mode,
            "model": model,
        }

        # Only include fields relevant to this event
        if event == "start":
            entry["query_preview"] = query_prev
            entry["profile"] = profile
        elif event == "complete":
            entry["iterations"] = iterations
            entry["tool_count"] = tool_count
            entry["tools_used"] = tools_used
            entry["total_duration_ms"] = total_duration_ms
        elif event == "error":
            entry["error_message"] = error_msg

        try:
            client = await self._get_async_client()
            if client is None:
                return None
            entry_id = await client.xadd("audit:agent_runs", entry)
            logger.debug("AuditLog agent run: %s/%s (%s)", session_id, event, entry_id)

            # Auto-trim if needed
            await self._maybe_trim_async(client)
            return entry_id
        except Exception as exc:
            logger.warning("AuditLog agent run failed: %s", exc)
            return None

    async def query_tool_executions(
        self,
        session_id: str | None = None,
        tool_name: str | None = None,
        since_ms: int | None = None,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        """Query tool executions from audit:tool_executions stream."""
        if not self._ready:
            return []

        try:
            client = await self._get_async_client()
            if client is None:
                return []

            # Fetch last N entries
            entries = await client.xrevrange("audit:tool_executions", count=count)
            results = []

            for entry_id, data in entries:
                # Apply time filter
                if since_ms:
                    try:
                        ts = datetime.fromisoformat(data.get("timestamp", ""))
                        now = datetime.now(timezone.utc)
                        age_ms = (now - ts).total_seconds() * 1000
                        if age_ms > since_ms:
                            continue
                    except Exception:
                        pass

                # Apply session filter
                if session_id and data.get("session_id") != session_id:
                    continue

                # Apply tool filter
                if tool_name and data.get("tool_name") != tool_name:
                    continue

                # Deserialize result_size and args_json
                try:
                    data["result_size"] = int(data.get("result_size", 0))
                except (ValueError, TypeError):
                    data["result_size"] = 0

                results.append(data)

            return results
        except Exception as exc:
            logger.warning("AuditLog query_tool_executions failed: %s", exc)
            return []

    async def query_agent_runs(
        self,
        session_id: str | None = None,
        since_ms: int | None = None,
        count: int = 100,
    ) -> list[dict[str, Any]]:
        """Query agent run events from audit:agent_runs stream."""
        if not self._ready:
            return []

        try:
            client = await self._get_async_client()
            if client is None:
                return []

            # Fetch last N entries
            entries = await client.xrevrange("audit:agent_runs", count=count)
            results = []

            for entry_id, data in entries:
                # Apply time filter
                if since_ms:
                    try:
                        ts = datetime.fromisoformat(data.get("timestamp", ""))
                        now = datetime.now(timezone.utc)
                        age_ms = (now - ts).total_seconds() * 1000
                        if age_ms > since_ms:
                            continue
                    except Exception:
                        pass

                # Apply session filter
                if session_id and data.get("session_id") != session_id:
                    continue

                # Convert numeric fields
                for field in ["iterations", "tool_count", "total_duration_ms"]:
                    try:
                        data[field] = int(data.get(field, 0))
                    except (ValueError, TypeError):
                        data[field] = 0

                results.append(data)

            return results
        except Exception as exc:
            logger.warning("AuditLog query_agent_runs failed: %s", exc)
            return []

    async def stats(self, since_ms: int | None = None) -> dict[str, Any]:
        """Generate audit log statistics."""
        if not self._ready:
            return {
                "status": "not_ready",
                "total_tool_calls": 0,
                "total_runs": 0,
            }

        try:
            client = await self._get_async_client()
            if client is None:
                return {"status": "error"}

            # Fetch all entries (no count limit for stats)
            tool_entries = await client.xrevrange("audit:tool_executions")
            run_entries = await client.xrevrange("audit:agent_runs")

            tool_calls = []
            error_count = 0
            tool_counts: dict[str, int] = {}
            total_duration = 0

            for entry_id, data in tool_entries:
                # Apply time filter
                if since_ms:
                    try:
                        ts = datetime.fromisoformat(data.get("timestamp", ""))
                        now = datetime.now(timezone.utc)
                        age_ms = (now - ts).total_seconds() * 1000
                        if age_ms > since_ms:
                            continue
                    except Exception:
                        pass

                tool_calls.append(data)
                tool_name = data.get("tool_name", "unknown")
                tool_counts[tool_name] = tool_counts.get(tool_name, 0) + 1

                if data.get("status") == "error":
                    error_count += 1

                try:
                    total_duration += int(data.get("duration_ms", 0))
                except (ValueError, TypeError):
                    pass

            # Count agent runs (complete events)
            run_count = 0
            for entry_id, data in run_entries:
                if since_ms:
                    try:
                        ts = datetime.fromisoformat(data.get("timestamp", ""))
                        now = datetime.now(timezone.utc)
                        age_ms = (now - ts).total_seconds() * 1000
                        if age_ms > since_ms:
                            continue
                    except Exception:
                        pass

                if data.get("event") == "complete":
                    run_count += 1

            # Build top_tools sorted by count
            top_tools = sorted(tool_counts.items(), key=lambda x: x[1], reverse=True)

            error_rate = error_count / len(tool_calls) if tool_calls else 0.0
            avg_duration = total_duration / len(tool_calls) if tool_calls else 0

            return {
                "status": "ready",
                "total_tool_calls": len(tool_calls),
                "total_runs": run_count,
                "error_count": error_count,
                "error_rate": error_rate,
                "avg_duration_ms": round(avg_duration, 1),
                "top_tools": top_tools,
                "stream_sizes": {
                    "tool_executions": len(tool_entries),
                    "agent_runs": len(run_entries),
                },
            }
        except Exception as exc:
            logger.warning("AuditLog stats failed: %s", exc)
            return {"status": "error"}

    async def stream_length(self, stream_key: str) -> int:
        """Get the number of entries in a stream."""
        if not self._ready:
            return 0

        try:
            client = await self._get_async_client()
            if client is None:
                return 0
            length = await client.xlen(stream_key)
            return length
        except Exception as exc:
            logger.warning("AuditLog stream_length failed for %s: %s", stream_key, exc)
            return 0

    async def _maybe_trim_async(self, client: Any) -> None:
        """Auto-trim streams if they exceed max_entries (async)."""
        async with self._lock:
            try:
                for stream_key in ["audit:tool_executions", "audit:agent_runs"]:
                    length = await client.xlen(stream_key)
                    if length > self._max_entries + self._trim_buffer:
                        await client.xtrim(stream_key, maxlen=self._max_entries, approximate=False)
                        logger.info(
                            "AuditLog trimmed %s to %d entries",
                            stream_key,
                            self._max_entries,
                        )
            except Exception as exc:
                logger.warning("AuditLog auto-trim failed: %s", exc)

    def trim(self, max_entries: int | None = None) -> dict[str, int]:
        """Manually trim both streams to max_entries (sync method for immediate cleanup)."""
        if not self._ready or self._redis_sync is None:
            return {}

        limit = max_entries or self._max_entries
        results = {}

        try:
            for stream_key in ["audit:tool_executions", "audit:agent_runs"]:
                count = self._redis_sync.xtrim(stream_key, maxlen=limit, approximate=False)
                results[stream_key] = count
                logger.info("AuditLog manually trimmed %s (removed %d entries)", stream_key, count)
        except Exception as exc:
            logger.warning("AuditLog manual trim failed: %s", exc)

        return results


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: AuditLog | None = None


def get_audit_log() -> AuditLog | None:
    """Get the singleton audit log instance."""
    return _instance


def init_audit_log(
    redis_url: str | None = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> AuditLog:
    """Initialize the singleton audit log."""
    global _instance
    _instance = AuditLog(redis_url=redis_url, max_entries=max_entries)
    return _instance
