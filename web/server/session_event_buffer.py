"""Redis-backed per-session buffer for ephemeral UI events.

Events like ``tool.call``, ``tool.calls.flush``, ``research.progress`` and
``research.activity`` are broadcast to the browser WebSocket but NOT
persisted to SQLite — they can be high-frequency during a research run.

If the user reloads the page mid-run (or hours after it completed but within
the TTL), the browser reconnects, reads the DB-backed history, and then
asks this buffer to replay any ephemeral events it has. The ToolCallsPanel
and research-activity panel reconstruct without having bloated the SQLite
database with dozens of transient rows per turn.

Keying:
  agentforge:session:events:{session_id}  — Redis list of JSON-encoded events

Policy:
  - RPUSH on every record (chronological order preserved)
  - LTRIM to the newest ``_MAX_EVENTS`` after every push
  - EXPIRE reset to ``_TTL_SECONDS`` after every push
  - No explicit clear on new job start — events from prior turns remain
    visible until TTL, which is the user-facing behavior we want
  - Decode errors are logged and skipped; a bad row never breaks replay
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any

from app.config import settings as _af_settings

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"

# Cap per-session buffer. 500 comfortably covers a 30-50 tool-call research
# run plus progress/activity events; older events trimmed first.
_MAX_EVENTS = _af_settings.memory.session_event_buffer_max_events
# Replayable window. 1h is generous for "I left the tab open yesterday";
# past that, SQLite-persisted summaries are the source of truth anyway.
_TTL_SECONDS = _af_settings.memory.session_event_buffer_ttl_seconds


def _key(session_id: str) -> str:
    return f"agentforge:session:events:{session_id}"


class SessionEventBuffer:
    """Lazy-initialised async Redis client wrapping the buffer operations."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._client = None
        self._lock = threading.Lock()

    def _get_client(self):
        # aioredis clients are bound to the event loop that created them, but
        # agentforge-web has a single uvicorn loop so one client is safe. Lazy init
        # avoids creating the connection at import time.
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            import redis.asyncio

            self._client = redis.asyncio.from_url(self._url, decode_responses=True)
            return self._client

    async def record(self, session_id: str, event: dict[str, Any]) -> None:
        """Append one event to the session buffer. Fire-and-forget."""
        if not session_id or not isinstance(event, dict):
            return
        try:
            payload = json.dumps(event, default=str)
        except Exception as exc:
            logger.debug("SessionEventBuffer: failed to serialise event: %s", exc)
            return
        client = self._get_client()
        try:
            # Pipeline the three ops so they round-trip together.
            async with client.pipeline(transaction=False) as pipe:
                pipe.rpush(_key(session_id), payload)
                pipe.ltrim(_key(session_id), -_MAX_EVENTS, -1)
                pipe.expire(_key(session_id), _TTL_SECONDS)
                await pipe.execute()
        except Exception as exc:
            logger.debug("SessionEventBuffer.record failed for %s: %s", session_id, exc)

    async def replay(self, session_id: str) -> list[dict[str, Any]]:
        """Return all buffered events for a session in chronological order."""
        if not session_id:
            return []
        client = self._get_client()
        try:
            raw = await client.lrange(_key(session_id), 0, -1)
        except Exception as exc:
            logger.debug("SessionEventBuffer.replay failed for %s: %s", session_id, exc)
            return []
        out: list[dict[str, Any]] = []
        for row in raw or []:
            try:
                out.append(json.loads(row))
            except Exception:
                continue  # skip malformed row, keep replaying the rest
        return out

    async def clear(self, session_id: str) -> None:
        """Explicitly drop a session's buffer. Optional — TTL handles it too."""
        if not session_id:
            return
        try:
            await self._get_client().delete(_key(session_id))
        except Exception as exc:
            logger.debug("SessionEventBuffer.clear failed for %s: %s", session_id, exc)


_buffer: SessionEventBuffer | None = None
_buffer_lock = threading.Lock()


def get_session_event_buffer() -> SessionEventBuffer:
    """Return the process-wide SessionEventBuffer singleton."""
    global _buffer
    if _buffer is not None:
        return _buffer
    with _buffer_lock:
        if _buffer is None:
            _buffer = SessionEventBuffer()
        return _buffer
