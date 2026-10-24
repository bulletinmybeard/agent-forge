"""Redis-backed cooldown for rate-limited search / fetch providers.

When a provider answers with a ``RateLimitError``, the fallback walk
marks it as cooling down so later calls skip it instead of paying a
wasted round trip per request. Lives in Redis (not SQLite) so the API
containers and the Mac worker share the same state; the key TTL ends
the cooldown on its own.

Keying:
    websearch:cooldown:{provider}  — per provider, not per operation
    (Tavily search + extract share one credit pool)

TTL policy (see ``cooldown_ttl``):
    - 432 (Tavily plan limit) — until the 1st of next month, 00:00 UTC
    - otherwise ``Retry-After`` if present, else 60s

Fail-open: any Redis error means "not cooling" and a no-op mark, so an
unreachable Redis never makes search worse than without the cooldown.
"""

from __future__ import annotations

import math
import os
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import redis
from chalkbox.logging.bridge import get_logger

from .base import RateLimitError

logger = get_logger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"
_KEY_PREFIX = "websearch:cooldown:"
_DEFAULT_TTL = 60
_PLAN_LIMIT_STATUS = 432
# Keep a dead Redis from stalling every search call.
_SOCKET_TIMEOUT = 1.0


def cooldown_until(ttl: int, now: datetime | None = None) -> str:
    return ((now or datetime.now(UTC)) + timedelta(seconds=ttl)).strftime("%Y-%m-%d %H:%M UTC")


def cooldown_ttl(exc: RateLimitError, now: datetime | None = None) -> int:
    if exc.status_code == _PLAN_LIMIT_STATUS:
        now = now or datetime.now(UTC)
        year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
        reset = datetime(year, month, 1, tzinfo=UTC)
        return max(math.ceil((reset - now).total_seconds()), 1)
    if exc.retry_after:
        return exc.retry_after
    return _DEFAULT_TTL


class ProviderCooldown:
    def __init__(self, redis_url: str | None = None, client: Any = None) -> None:
        self._url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._client = client
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        with self._lock:
            if self._client is None:
                self._client = redis.from_url(
                    self._url,
                    decode_responses=True,
                    socket_connect_timeout=_SOCKET_TIMEOUT,
                    socket_timeout=_SOCKET_TIMEOUT,
                )
            return self._client

    def remaining(self, provider: str) -> int | None:
        """Seconds left on the cooldown, or ``None`` when not cooling."""
        try:
            ttl = self._get_client().ttl(_KEY_PREFIX + provider)
        except Exception as exc:
            logger.debug("[cooldown] check failed for %r: %s", provider, exc)
            return None
        # -2 = no key, -1 = key without expiry (shouldn't happen, treat as not cooling)
        return int(ttl) if ttl is not None and ttl > 0 else None

    def mark(self, provider: str, ttl: int, reason: str) -> None:
        try:
            self._get_client().set(_KEY_PREFIX + provider, reason, ex=ttl)
        except Exception as exc:
            logger.debug("[cooldown] mark failed for %r: %s", provider, exc)
            return
        logger.warning("[cooldown] provider %r cooling down for %ds (%s)", provider, ttl, reason)


_cooldown: ProviderCooldown | None = None
_cooldown_lock = threading.Lock()


def get_provider_cooldown() -> ProviderCooldown:
    """Return the process-wide ``ProviderCooldown`` singleton."""
    global _cooldown
    if _cooldown is not None:
        return _cooldown
    with _cooldown_lock:
        if _cooldown is None:
            _cooldown = ProviderCooldown()
        return _cooldown


__all__ = ["ProviderCooldown", "cooldown_ttl", "cooldown_until", "get_provider_cooldown"]
