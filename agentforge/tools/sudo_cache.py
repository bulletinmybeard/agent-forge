"""In-memory, TTL-bounded store for an interactively-entered sudo password.

Memory-only. Never persisted, logged, or serialized. Keyed by a label
(e.g., "localhost") so a future per-host ssh use can reuse the type.
"""

from __future__ import annotations

import time
from collections.abc import Callable


class SudoCredentialCache:
    def __init__(self, ttl_seconds: int = 300, clock: Callable[[], float] = time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._entries: dict[str, tuple[str, float]] = {}

    def get(self, label: str) -> str | None:
        entry = self._entries.get(label)
        if entry is None:
            return None
        secret, last_used = entry
        now = self._clock()
        if now - last_used > self._ttl:
            self._entries.pop(label, None)
            return None
        self._entries[label] = (secret, now)  # sliding TTL
        return secret

    def set(self, label: str, secret: str) -> None:
        self._entries[label] = (secret, self._clock())

    def invalidate(self, label: str) -> None:
        self._entries.pop(label, None)

    def clear(self) -> None:
        self._entries.clear()
