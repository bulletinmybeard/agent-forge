"""Redis-backed burst-ID → snapshot-IDs map for ``@coding`` undo.

Each ``@coding`` apply produces N per-file snapshots (via the existing on-disk ``_file_snapshots`` store,
keyed by pre_hash). Rather than storing the full snapshot list in the session's DB (context-window clutter),
we map a short burst-ID to the list of snapshot IDs here.

Keying:
    coding:burst:{session_id}:{burst_id}  — Redis list of snapshot IDs

Policy:
    - RPUSH the snapshot IDs on create, EXPIRE per ``snapshot_ttl_seconds``.
    - LRANGE on undo to fetch the full list.
    - DELETE after undo is applied (idempotent).
    - Per-burst keys — collisions only possible within a single session,
      and ``burst_id = uuid[:12]`` gives us 48 bits of entropy.

Uses the sync ``redis`` client, not ``redis.asyncio``. ``code_apply`` and ``code_undo``
both run inside ``asyncio.to_thread`` (sync context) and previously span up a throwaway event loop to drive the async client — but since the client is a process-wide singleton, the first loop would close and subsequent calls would silently fail against the dead loop.
Sync client sidesteps the whole event-loop-binding problem.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from chalkbox.logging.bridge import get_logger

logger = get_logger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"


def _key(session_id: str, burst_id: str) -> str:
    return f"coding:burst:{session_id}:{burst_id}"


class RollbackStore:
    """Lazy singleton wrapping the sync Redis ops for coding-mode undo."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._client: Any = None
        self._lock = threading.Lock()

    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is not None:
                return self._client
            import redis

            self._client = redis.from_url(self._url, decode_responses=True)
            return self._client

    def store_burst(
        self,
        session_id: str,
        burst_id: str,
        snapshot_ids: list[str],
        ttl_seconds: int = 86400,
    ) -> None:
        """Persist the snapshot-ID list for a burst. Fire-and-forget."""
        if not session_id or not burst_id or not snapshot_ids:
            return
        client = self._get_client()
        try:
            pipe = client.pipeline(transaction=False)
            pipe.rpush(_key(session_id, burst_id), *snapshot_ids)
            pipe.expire(_key(session_id, burst_id), ttl_seconds)
            pipe.execute()
        except Exception as exc:
            logger.debug(
                "RollbackStore.store_burst failed for %s/%s: %s",
                session_id,
                burst_id,
                exc,
            )

    def load_burst(self, session_id: str, burst_id: str) -> list[str]:
        """Return the snapshot-ID list for a burst. Empty list on miss."""
        if not session_id or not burst_id:
            return []
        try:
            raw = self._get_client().lrange(_key(session_id, burst_id), 0, -1)
            return list(raw or [])
        except Exception as exc:
            logger.debug(
                "RollbackStore.load_burst failed for %s/%s: %s",
                session_id,
                burst_id,
                exc,
            )
            return []

    def delete_burst(self, session_id: str, burst_id: str) -> None:
        """Drop the burst entry (called after a successful undo)."""
        if not session_id or not burst_id:
            return
        try:
            self._get_client().delete(_key(session_id, burst_id))
        except Exception as exc:
            logger.debug(
                "RollbackStore.delete_burst failed for %s/%s: %s",
                session_id,
                burst_id,
                exc,
            )


_store: RollbackStore | None = None
_store_lock = threading.Lock()


def get_rollback_store() -> RollbackStore:
    """Return the process-wide ``RollbackStore`` singleton."""
    global _store
    if _store is not None:
        return _store
    with _store_lock:
        if _store is None:
            _store = RollbackStore()
        return _store


__all__ = ["RollbackStore", "get_rollback_store"]
