"""Redis Streams-based telemetry for the mode classifier.

Records every classification verdict — which layer decided, what the
heuristic + LLM picked, what final mode the runner dispatched on. Lets
us measure classifier accuracy from production data without bolting on
ad-hoc logging.

Mirrors the shape of ``audit_log.py`` — same Redis stream + auto-trim
pattern, separate stream key so audit consumers don't have to filter.

**Stream key:** ``audit:classifier_verdicts``

**Schema (every XADD entry):**

- ``timestamp`` — ISO 8601 UTC
- ``session_id`` — empty when not bound to a session yet
- ``query_hash`` — sha256[:16] of the query (privacy-friendly join key)
- ``query_preview`` — first 200 chars of the query
- ``query_len_words`` — word count (for sticky-mode debugging)
- ``last_mode`` — the previous mode, drives sticky decisions
- ``layer`` — which layer decided: ``custom_alias`` | ``explicit_prefix``
  | ``heuristic_keyword`` | ``heuristic_pattern`` | ``sticky`` |
  ``llm`` | ``fallback_chat`` | ``unknown_prefix``
- ``heuristic_mode`` — what ``_classify_mode_heuristic`` returned
  (always set; "chat" when ambiguous)
- ``heuristic_confidence`` — "high" | "medium" | "low" — drives the
  LLM escalation tier
- ``llm_mode`` — what the LLM picked when escalated, empty otherwise
- ``llm_profile`` — name of the AI profile that drove the LLM call
  (e.g., "fast", "cloud-light"); empty when LLM wasn't consulted
- ``llm_model`` — concrete model string at call time (e.g.,
  ``ministral-3:14b-cloud``); empty when LLM wasn't consulted
- ``llm_provider`` — provider tag (ollama/deepinfra/bedrock/openrouter)
- ``final_mode`` — the mode the runner actually dispatched on
- ``latency_ms`` — total wall time of the classification path

**Usage:**

    from web.server.classifier_audit import get_classifier_audit

    audit = get_classifier_audit()
    await audit.log_verdict(
        session_id="sess-123",
        query="restart nginx",
        last_mode="chat",
        layer="heuristic_keyword",
        heuristic_mode="agent",
        llm_mode=None,
        final_mode="agent",
        latency_ms=1,
    )

Fire-and-forget by design: Redis unavailability never affects the
classifier path — the helper returns ``None`` and logs at DEBUG.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_REDIS_URL = "redis://localhost:6379"
_DEFAULT_MAX_ENTRIES = 50000
_TRIM_THRESHOLD = 50001

STREAM_KEY = "audit:classifier_verdicts"

# Layer labels — keep in lockstep with the classifier's own logging so
# that grepping production logs lines up with telemetry entries.
LAYER_CUSTOM_ALIAS = "custom_alias"
LAYER_EXPLICIT_PREFIX = "explicit_prefix"
LAYER_HEURISTIC_KEYWORD = "heuristic_keyword"
LAYER_HEURISTIC_PATTERN = "heuristic_pattern"
LAYER_STICKY = "sticky"
LAYER_LLM = "llm"
LAYER_FALLBACK_CHAT = "fallback_chat"
LAYER_UNKNOWN_PREFIX = "unknown_prefix"
LAYER_USER_OVERRIDE = "user_override"

_VALID_LAYERS = frozenset(
    {
        LAYER_CUSTOM_ALIAS,
        LAYER_EXPLICIT_PREFIX,
        LAYER_HEURISTIC_KEYWORD,
        LAYER_HEURISTIC_PATTERN,
        LAYER_STICKY,
        LAYER_LLM,
        LAYER_FALLBACK_CHAT,
        LAYER_UNKNOWN_PREFIX,
        LAYER_USER_OVERRIDE,
    }
)


class ClassifierAudit:
    """Redis Streams telemetry for classifier verdicts."""

    def __init__(
        self,
        redis_url: str | None = None,
        max_entries: int = _DEFAULT_MAX_ENTRIES,
    ) -> None:
        self._redis_url = redis_url or os.environ.get("REDIS_URL", _DEFAULT_REDIS_URL)
        self._max_entries = max_entries
        self._redis_async: Any = None
        self._redis_sync: Any = None
        self._ready = False
        self._connect()

    def _connect(self) -> None:
        try:
            import redis

            self._redis_sync = redis.from_url(self._redis_url, decode_responses=True)
            self._redis_sync.ping()
            self._ready = True
            logger.info("ClassifierAudit connected to Redis at %s", self._redis_url)
        except Exception as exc:
            logger.warning(
                "ClassifierAudit Redis connection failed: %s — telemetry disabled",
                exc,
            )
            self._ready = False

    async def _get_async_client(self) -> Any:
        if self._redis_async is None and self._ready:
            try:
                import redis.asyncio

                self._redis_async = redis.asyncio.from_url(
                    self._redis_url,
                    decode_responses=True,
                )
                await self._redis_async.ping()
            except Exception as exc:
                logger.warning("ClassifierAudit async client failed: %s", exc)
                self._redis_async = None
        return self._redis_async

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _hash_query(query: str) -> str:
        return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]

    async def log_verdict(
        self,
        *,
        session_id: str = "",
        query: str,
        last_mode: str = "",
        layer: str,
        heuristic_mode: str = "",
        heuristic_confidence: str = "",
        llm_mode: str | None = None,
        llm_profile: str = "",
        llm_model: str = "",
        llm_provider: str = "",
        final_mode: str,
        latency_ms: int = 0,
    ) -> str | None:
        """Persist a classifier verdict. Fire-and-forget.

        Returns the Redis stream entry ID or ``None`` if telemetry is
        disabled / failed. Never raises — telemetry must not break the
        classifier path.
        """
        if not self._ready:
            return None

        if layer not in _VALID_LAYERS:
            logger.debug("ClassifierAudit: unknown layer %r — recording anyway", layer)

        preview = (query or "")[:200]
        words = len((query or "").split())

        entry = {
            "timestamp": self._now_iso(),
            "session_id": session_id or "",
            "query_hash": self._hash_query(query or ""),
            "query_preview": preview,
            "query_len_words": words,
            "last_mode": last_mode or "",
            "layer": layer,
            "heuristic_mode": heuristic_mode or "",
            "heuristic_confidence": heuristic_confidence or "",
            "llm_mode": llm_mode or "",
            "llm_profile": llm_profile or "",
            "llm_model": llm_model or "",
            "llm_provider": llm_provider or "",
            "final_mode": final_mode,
            "latency_ms": int(latency_ms),
        }

        try:
            client = await self._get_async_client()
            if client is None:
                return None
            entry_id = await client.xadd(STREAM_KEY, entry)
            await self._maybe_trim_async(client)
            return entry_id
        except Exception as exc:
            logger.debug("ClassifierAudit log_verdict failed: %s", exc)
            return None

    async def log_override(
        self,
        *,
        session_id: str = "",
        query: str,
        original_mode: str,
        override_mode: str,
    ) -> str | None:
        """Persist a user-initiated mode override — "ground truth" label.

        Records when a user clicked the Router → [mode] chip and picked a
        different mode than the classifier chose. These rows are the
        labelled training signal for tuning the heuristic / LLM balance
        later — every override is direct user feedback that the chosen
        mode was wrong for this prompt.

        Stored in the same Redis stream as regular verdicts but tagged
        with ``layer="user_override"`` and the original-vs-override modes
        captured in dedicated fields so consumers can filter cleanly.
        """
        if not self._ready:
            return None

        preview = (query or "")[:200]
        words = len((query or "").split())

        entry = {
            "timestamp": self._now_iso(),
            "session_id": session_id or "",
            "query_hash": self._hash_query(query or ""),
            "query_preview": preview,
            "query_len_words": words,
            "last_mode": "",
            "layer": "user_override",
            "heuristic_mode": "",
            "heuristic_confidence": "",
            "llm_mode": "",
            "llm_profile": "",
            "llm_model": "",
            "llm_provider": "",
            "final_mode": override_mode,
            "original_mode": original_mode or "",
            "override_mode": override_mode,
            "latency_ms": 0,
        }

        try:
            client = await self._get_async_client()
            if client is None:
                return None
            entry_id = await client.xadd(STREAM_KEY, entry)
            await self._maybe_trim_async(client)
            return entry_id
        except Exception as exc:
            logger.debug("ClassifierAudit log_override failed: %s", exc)
            return None

    async def _maybe_trim_async(self, client: Any) -> None:
        try:
            length = await client.xlen(STREAM_KEY)
            if length >= _TRIM_THRESHOLD:
                await client.xtrim(STREAM_KEY, maxlen=self._max_entries, approximate=True)
        except Exception as exc:
            logger.debug("ClassifierAudit trim failed: %s", exc)

    # --- Convenience: synchronous wrapper for non-async call sites -----

    def log_verdict_sync(self, **kwargs: Any) -> None:
        """Sync fire-and-forget — schedules the async log via the running
        loop if any, otherwise drops. Useful from sync code paths that
        already have an event loop available.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.log_verdict(**kwargs))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_instance: ClassifierAudit | None = None


def get_classifier_audit() -> ClassifierAudit | None:
    """Return the singleton, or ``None`` if not initialised."""
    return _instance


def init_classifier_audit(
    redis_url: str | None = None,
    max_entries: int = _DEFAULT_MAX_ENTRIES,
) -> ClassifierAudit:
    """Initialise the singleton. Idempotent — re-init replaces."""
    global _instance
    _instance = ClassifierAudit(redis_url=redis_url, max_entries=max_entries)
    return _instance


__all__ = [
    "ClassifierAudit",
    "STREAM_KEY",
    "LAYER_CUSTOM_ALIAS",
    "LAYER_EXPLICIT_PREFIX",
    "LAYER_HEURISTIC_KEYWORD",
    "LAYER_HEURISTIC_PATTERN",
    "LAYER_STICKY",
    "LAYER_LLM",
    "LAYER_FALLBACK_CHAT",
    "LAYER_UNKNOWN_PREFIX",
    "LAYER_USER_OVERRIDE",
    "get_classifier_audit",
    "init_classifier_audit",
]
