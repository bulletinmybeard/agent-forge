"""Session Event Publishing & Subscription via Redis Pub/Sub.

Broadcasts real-time session status across all connected AgentForge clients.
Enables live visibility into what other clients are doing (session start/complete,
indexing events, system notifications) via Redis Pub/Sub channels.

Usage:
    # Publishing (sync-safe for use in non-async contexts)
    from web.server.session_events import get_session_event_publisher
    publisher = get_session_event_publisher()
    publisher.publish_sync("agentforge:sessions", {
        "event_type": "run_completed",
        "session_id": "...",
        "mode": "agent",
        "duration_ms": 4523,
        ...
    })

    # Subscribing (async for WebSocket consumers)
    from web.server.session_events import SessionEventSubscriber
    subscriber = SessionEventSubscriber(redis_url="redis://localhost:6379")
    await subscriber.subscribe("agentforge:sessions", "agentforge:system")
    async for event in subscriber.events():
        if event:  # None on timeout
            print(f"Received: {event['event_type']}")
        await ws.send_json(session_broadcast(**event))
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, AsyncGenerator

from app.config import settings as _af_settings

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"
_EVENT_TIMEOUT = _af_settings.memory.session_events_event_timeout  # seconds between keepalive checks
_RECONNECT_DELAY = _af_settings.memory.session_events_reconnect_delay  # seconds before reconnecting on error
_MAX_RECONNECT_ATTEMPTS = _af_settings.memory.session_events_max_reconnect_attempts


# ---------------------------------------------------------------------------
# Event message construction helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    """Return current ISO 8601 timestamp."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def session_broadcast(
    event_type: str,
    session_id: str,
    timestamp: str | None = None,
    **data: Any,
) -> dict:
    """Construct a session.broadcast WebSocket message."""
    return {
        "type": "session.broadcast",
        "event_type": event_type,
        "session_id": session_id,
        "timestamp": timestamp or _now(),
        **data,
    }


def system_broadcast(
    event_type: str,
    timestamp: str | None = None,
    **data: Any,
) -> dict:
    """Construct a system.broadcast WebSocket message."""
    return {
        "type": "system.broadcast",
        "event_type": event_type,
        "timestamp": timestamp or _now(),
        **data,
    }


# ---------------------------------------------------------------------------
# Publisher (sync-safe, fire-and-forget)
# ---------------------------------------------------------------------------


class SessionEventPublisher:
    """Publishes session and system events to Redis Pub/Sub channels.

    Uses a sync Redis client (redis.StrictRedis) for use in non-async contexts.
    All publish operations are fire-and-forget; failures are logged but never raise.
    This allows publishing from FastAPI sync endpoints, tool results, etc.

    Channels:
        agentforge:sessions  — Session lifecycle events (start/complete/error)
        agentforge:system    — System-wide events (indexing, config reloads, etc.)
        agentforge:session:{session_id}  — Per-session targeted notifications
    """

    def __init__(self, redis_url: str | None = None) -> None:
        """Initialize publisher with optional Redis URL override."""
        self._redis = None
        self._url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._ready = False
        self._connect()

    def _connect(self) -> None:
        """Connect to Redis (non-blocking, failures logged only)."""
        try:
            import redis

            self._redis = redis.from_url(self._url, decode_responses=True)
            self._redis.ping()
            self._ready = True
            logger.info("SessionEventPublisher connected to Redis at %s", self._url)
        except Exception as exc:
            logger.warning(
                "SessionEventPublisher Redis connection failed: %s — publishing disabled",
                exc,
            )
            self._ready = False

    async def publish_session_event(self, event_type: str, session_id: str, **data: Any) -> None:
        """Publish a session event to agentforge:sessions channel.

        Example:
            await publisher.publish_session_event(
                event_type="run_completed",
                session_id="019d14f2-...",
                mode="agent",
                model="mistral-large:3",
                query_preview="Check Docker status",
                duration_ms=4523,
                tool_count=3,
                tools_used=["docker_ps", "shell"],
                status="success",
            )
        """
        if not self._ready:
            logger.debug("SessionEventPublisher not ready, skipping publish_session_event")
            return

        message: dict[str, Any] = {
            "event_type": event_type,
            "session_id": session_id,
            "timestamp": _now(),
        }
        message.update(data)

        try:
            channel = "agentforge:sessions"
            self._redis.publish(channel, json.dumps(message))
            logger.debug(
                "Published session event: %s (session=%s)",
                event_type,
                session_id,
            )
        except Exception as exc:
            logger.warning("Failed to publish session event: %s", exc)

    async def publish_system_event(self, event_type: str, **data: Any) -> None:
        """Publish a system event to agentforge:system channel.

        Example:
            await publisher.publish_system_event(
                event_type="indexing_complete",
                collection="agentforge_kb",
                chunks_indexed=1250,
                duplicates_found=47,
                drift_count=12,
                duration_ms=8234,
            )
        """
        if not self._ready:
            logger.debug("SessionEventPublisher not ready, skipping publish_system_event")
            return

        message: dict[str, Any] = {
            "event_type": event_type,
            "timestamp": _now(),
        }
        message.update(data)

        try:
            channel = "agentforge:system"
            self._redis.publish(channel, json.dumps(message))
            logger.debug("Published system event: %s", event_type)
        except Exception as exc:
            logger.warning("Failed to publish system event: %s", exc)

    async def publish_to_session(self, session_id: str, event_type: str, **data: Any) -> None:
        """Send a targeted event to a specific session's channel.

        Example:
            await publisher.publish_to_session(
                session_id="019d14f2-...",
                event_type="cache_hit",
                tool="web_search",
                query="python 3.12",
                saved_ms=234,
            )
        """
        if not self._ready:
            logger.debug("SessionEventPublisher not ready, skipping publish_to_session")
            return

        message: dict[str, Any] = {
            "event_type": event_type,
            "session_id": session_id,
            "timestamp": _now(),
        }
        message.update(data)

        try:
            channel = f"agentforge:session:{session_id}"
            self._redis.publish(channel, json.dumps(message))
            logger.debug(
                "Published targeted event to %s: %s",
                channel,
                event_type,
            )
        except Exception as exc:
            logger.warning("Failed to publish to session: %s", exc)

    def publish_sync(self, channel: str, message: dict) -> None:
        """Sync wrapper for non-async contexts (fire-and-forget).

        Use this in sync functions or sync tool implementations.

        Example:
            publisher.publish_sync("agentforge:system", {
                "event_type": "config_reloaded",
                "timestamp": "2026-03-22T14:30:00Z",
            })
        """
        if not self._ready:
            logger.debug("SessionEventPublisher not ready, skipping publish_sync")
            return

        try:
            self._redis.publish(channel, json.dumps(message))
            logger.debug("Published sync event to %s", channel)
        except Exception as exc:
            logger.warning("Failed to publish sync event: %s", exc)


# ---------------------------------------------------------------------------
# Subscriber (async generator for WebSocket consumers)
# ---------------------------------------------------------------------------


class SessionEventSubscriber:
    """Subscribes to Redis Pub/Sub and yields events as async iterator.

    Automatically reconnects on Redis disconnection.
    Yields None on timeout (every 1s) for keepalive/health checks.

    Usage:
        subscriber = SessionEventSubscriber()
        await subscriber.subscribe("agentforge:sessions", "agentforge:system")
        async for event in subscriber.events():
            if event:  # Skip None keepalive
                await ws.send_json(event)
    """

    def __init__(self, redis_url: str | None = None) -> None:
        """Initialize subscriber with optional Redis URL override."""
        self._url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._redis = None
        self._pubsub = None
        self._subscribed_channels: set[str] = set()
        self._connected = False
        self._reconnect_count = 0

    async def _connect(self) -> bool:
        """Connect to Redis asynchronously."""
        try:
            import redis.asyncio

            self._redis = redis.asyncio.from_url(self._url, decode_responses=True)
            await self._redis.ping()
            self._pubsub = self._redis.pubsub()
            self._connected = True
            self._reconnect_count = 0
            logger.info("SessionEventSubscriber connected to Redis at %s", self._url)
            return True
        except Exception as exc:
            logger.warning(
                "SessionEventSubscriber connection failed (attempt %d/%d): %s",
                self._reconnect_count + 1,
                _MAX_RECONNECT_ATTEMPTS,
                exc,
            )
            self._reconnect_count += 1
            self._connected = False
            return False

    async def subscribe(self, *channels: str) -> None:
        """Subscribe to one or more channels.

        Raises:
            RuntimeError: If unable to connect after max attempts.
        """
        for attempt in range(_MAX_RECONNECT_ATTEMPTS):
            if await self._connect():
                break
            if attempt < _MAX_RECONNECT_ATTEMPTS - 1:
                await asyncio.sleep(_RECONNECT_DELAY)
        else:
            raise RuntimeError(f"SessionEventSubscriber failed to connect after {_MAX_RECONNECT_ATTEMPTS} attempts")

        for channel in channels:
            await self._pubsub.subscribe(channel)
            self._subscribed_channels.add(channel)
            logger.info("Subscribed to channel: %s", channel)

    async def events(self) -> AsyncGenerator[dict[str, Any] | None, None]:
        """Async generator yielding parsed events from subscribed channels.

        Each event is a dict: {channel, event_type, session_id, timestamp, ...data}
        Yields None on timeout (every 1s) for keepalive/health checks.

        Handles disconnection and auto-reconnect gracefully.

        Example:
            async for event in subscriber.events():
                if event:
                    print(f"Event: {event['event_type']}")
                else:
                    print("Keepalive tick")
        """
        if not self._connected:
            raise RuntimeError("Subscriber not connected. Call subscribe() first.")

        last_reconnect = 0.0

        while True:
            try:
                # get_message with timeout blocks up to _EVENT_TIMEOUT waiting for
                # a message. Wrapping a default-timeout (non-blocking) get_message
                # in asyncio.wait_for was a busy-loop — the inner call returned
                # None immediately, so the loop spun at scheduler speed.
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=_EVENT_TIMEOUT,
                )

                if message:
                    try:
                        data = json.loads(message["data"])
                        data["channel"] = message["channel"]
                        yield data
                    except json.JSONDecodeError as exc:
                        logger.warning("Failed to parse event JSON: %s", exc)
                        continue
                else:
                    # No message within the timeout — keepalive tick
                    yield None

            except asyncio.CancelledError:
                # Propagate cancellation so the consumer can unwind.
                raise

            except Exception as exc:
                logger.warning("Error in subscriber event loop: %s", exc)

                now = asyncio.get_event_loop().time()
                if now - last_reconnect > _RECONNECT_DELAY and await self._connect():
                    for channel in self._subscribed_channels:
                        await self._pubsub.subscribe(channel)
                    logger.info("Reconnected and re-subscribed to all channels")
                    last_reconnect = now
                else:
                    await asyncio.sleep(_RECONNECT_DELAY)
                yield None

    async def unsubscribe(self) -> None:
        """Clean up subscription and close connection."""
        try:
            if self._pubsub:
                await self._pubsub.close()
            if self._redis:
                await self._redis.close()
            self._connected = False
            logger.info("SessionEventSubscriber unsubscribed and closed")
        except Exception as exc:
            logger.warning("Error closing subscriber: %s", exc)


# ---------------------------------------------------------------------------
# Singleton Publisher
# ---------------------------------------------------------------------------

_publisher_instance: SessionEventPublisher | None = None


def get_session_event_publisher() -> SessionEventPublisher | None:
    """Get the global session event publisher singleton.

    Example:
        publisher = get_session_event_publisher()
        if publisher:
            publisher.publish_sync("agentforge:system", {...})
    """
    return _publisher_instance


def init_session_event_publisher(redis_url: str | None = None) -> SessionEventPublisher:
    """Initialize the global session event publisher singleton.

    Should be called once during app startup.

    Example:
        publisher = init_session_event_publisher()
    """
    global _publisher_instance
    _publisher_instance = SessionEventPublisher(redis_url=redis_url)
    return _publisher_instance
