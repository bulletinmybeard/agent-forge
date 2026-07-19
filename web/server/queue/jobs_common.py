"""Shared, queue-agnostic helpers for SAQ worker job functions.

- HTTP callback plumbing (``_post_status``, ``_check_cancelled_http``, …).
- ``HttpCallbackSocket`` — WebSocket stub that POSTs durable + ephemeral
  events to agentforge-web.
- ``HttpConfirmationBroker`` — confirmation prompt over HTTP broadcast +
  poll, with auto-accept support.
- ``_NullDatabase`` — stub ChatDatabase that proxies all writes via HTTP so
  workers never touch SQLite directly.
- ``SaqCancelEvent`` — async-friendly cancel event for SAQ jobs (prefers
  SAQ native abort, falls back to HTTP poll).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid as _uuid
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Silence httpx's per-request INFO logs ("HTTP Request: POST … 200 OK").
# Internal agentforge-web callbacks are high-frequency and add no diagnostic value.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

AGENTFORGE_WEB_URL = os.environ.get("AGENTFORGE_WEB_URL", "http://localhost:8200")

# Shared secret for worker -> web /internal callbacks. When set (both sides read
# the same env), the web side requires it on /internal/* — defence-in-depth on
# top of the Traefik path-exclusion so the callbacks aren't forgeable if ever
# exposed. Empty = disabled (network isolation only, as before).
INTERNAL_TOKEN = os.environ.get("AGENTFORGE_INTERNAL_TOKEN", "").strip()


def internal_auth_headers() -> dict[str, str]:
    """Header carrying the /internal shared secret, when configured."""
    return {"X-Internal-Token": INTERNAL_TOKEN} if INTERNAL_TOKEN else {}


# ---------------------------------------------------------------------------
# Persistent HTTP clients (TCP/TLS reuse across all callbacks).
# ---------------------------------------------------------------------------

_sync_http: httpx.Client | None = None
_sync_http_lock = threading.Lock()


def _get_sync_http() -> httpx.Client:
    """Return a shared synchronous httpx.Client (lazy singleton)."""
    global _sync_http
    if _sync_http is not None:
        return _sync_http
    with _sync_http_lock:
        if _sync_http is not None:
            return _sync_http
        _sync_http = httpx.Client(
            base_url=AGENTFORGE_WEB_URL,
            timeout=5.0,
            headers=internal_auth_headers(),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        return _sync_http


# Async clients are loop-bound — keep one per event loop id.
_async_clients: dict[int, httpx.AsyncClient] = {}
_async_clients_lock = threading.Lock()


def _get_async_http() -> httpx.AsyncClient:
    """Return a shared async httpx.Client for the current event loop."""
    loop = asyncio.get_event_loop()
    lid = id(loop)
    client = _async_clients.get(lid)
    if client is not None and not client.is_closed:
        return client
    with _async_clients_lock:
        client = _async_clients.get(lid)
        if client is not None and not client.is_closed:
            return client
        client = httpx.AsyncClient(
            base_url=AGENTFORGE_WEB_URL,
            timeout=10.0,
            headers=internal_auth_headers(),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        _async_clients[lid] = client
        return client


# ---------------------------------------------------------------------------
# agentforge-web HTTP callbacks.
# ---------------------------------------------------------------------------


def _post_status(job_id: str, status: str, error: str | None = None) -> None:
    """Update job status in agentforge-web's job_store via HTTP (sync, fire-and-forget)."""
    payload: dict = {"status": status}
    if error:
        payload["error"] = error
    try:
        _get_sync_http().post(f"/internal/jobs/{job_id}/status", json=payload)
    except Exception as exc:
        logger.warning("Failed to POST job status %s -> %s: %s", job_id, status, exc)


def _check_cancelled_http(job_id: str) -> bool:
    """Ask agentforge-web whether this job has been cancelled (sync HTTP poll)."""
    try:
        resp = _get_sync_http().get(f"/internal/jobs/{job_id}/cancelled")
        return resp.json().get("cancelled", False)
    except Exception:
        return False


def _check_already_done_http(job_id: str) -> bool:
    """Ask agentforge-web whether this job is already in a terminal state."""
    try:
        resp = _get_sync_http().get(f"/internal/jobs/{job_id}/cancelled")
        return resp.json().get("done", False)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# HttpCallbackSocket — WebSocket stub for worker-side runs.
# ---------------------------------------------------------------------------


class HttpCallbackSocket:
    """WebSocket stub for worker-mode agent runs.

    Most events are persisted (and broadcast) via _NullDatabase.add_message()
    which POSTs to /internal/sessions/{id}/event.

    Ephemeral live-UI events are NOT persisted to DB but must still reach the
    browser (and CLI clients like Felix) during live processing. send_json()
    forwards those event types to the /broadcast endpoint so the ToolCallsPanel
    animates in real time without creating duplicate DB rows.

    agent.tool_exec (start/done, optional truncated output) is included so
    verbose clients can show tool results without waiting for the final report.
    """

    _BROADCAST_TYPES = frozenset(
        {
            "tool.call",
            "tool.calls.flush",
            "agent.tool_exec",
            "agent.iteration",
            "agent.thinking",
            "research.progress",
            "research.activity",
            "context.usage",  # ephemeral token-usage update — never stored
        }
    )

    def __init__(self, session_id: str, job_id: str) -> None:
        self._session_id = session_id
        self._job_id = job_id

    async def send_json(self, data: Any) -> None:
        """Forward an ephemeral UI event to the browser via /broadcast.

        Each event POSTs directly — no local buffering. Earlier we buffered
        tool.call events and only flushed them when tool.calls.flush arrived,
        to guarantee ordering at the WS endpoint. That made the live panel
        invisible whenever the flush coroutine got dropped (busy loop,
        exception swallowed at DEBUG, race with run completion). Posting
        immediately sacrifices strict ordering across concurrent parallel
        tool calls (rare) for guaranteed delivery of every event.
        """
        if not isinstance(data, dict):
            return
        event_type = data.get("type")
        if event_type not in self._BROADCAST_TYPES:
            return  # all other events are handled by _NullDatabase.add_message()

        url = f"/internal/sessions/{self._session_id}/broadcast"
        try:
            client = _get_async_http()
            await client.post(url, json=data)
        except Exception as exc:
            logger.debug(
                "Failed to broadcast %s for session %s: %s",
                event_type,
                self._session_id,
                exc,
            )

    async def receive_json(self) -> dict:
        # Block forever — runners that await incoming messages will time out.
        await asyncio.sleep(86400)
        return {}

    @property
    def client_state(self):
        """Mimic starlette WebSocket — always 'disconnected'."""
        return type("State", (), {"CONNECTED": 1, "value": 0})()


# ---------------------------------------------------------------------------
# HttpConfirmationBroker — confirmation prompts over HTTP broadcast + poll.
# ---------------------------------------------------------------------------


class HttpConfirmationBroker:
    """Confirmation broker for worker jobs.

    Bridges the gap between the synchronous agent thread and the browser UI
    via HTTP broadcast (request) + HTTP polling (response).
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        # Mirrors ConfirmationBroker.auto_accept — set True on first
        # "Yes (all)" click; subsequent requests are auto-confirmed.
        self.auto_accept: bool = False

    async def request(self, prompt: str) -> bool:
        """Broadcast a confirm.request to the browser and poll for the answer."""
        request_id = f"cr_{_uuid.uuid4().hex[:8]}"
        msg: dict = {"type": "confirm.request", "request_id": request_id, "prompt": prompt}

        if self.auto_accept:
            msg["auto_accepted"] = True
            await self._broadcast(msg)
            return True

        await self._broadcast(msg)

        # Poll for the user's response.
        # 290s deadline is intentionally shorter than make_sync_confirm_handler's
        # outer 300s timeout so this always returns explicitly before fail-open.
        poll_url = f"/internal/sessions/{self._session_id}/confirm/{request_id}"
        deadline = time.monotonic() + 290
        _warned_404 = False
        while time.monotonic() < deadline:
            await asyncio.sleep(0.5)
            try:
                resp = await _get_async_http().get(poll_url)

                if resp.status_code == 404:
                    if not _warned_404:
                        logger.warning(
                            "[confirm] /confirm endpoint returned 404 for session %s"
                            " — agentforge-web may need to be rebuilt (docker compose up --build)."
                            " Polling will continue until timeout, then deny.",
                            self._session_id,
                        )
                        _warned_404 = True
                    continue

                data = resp.json()
                if data.get("ready"):
                    if data.get("auto_accept"):
                        self.auto_accept = True
                        logger.info(
                            "[confirm] Auto-accept enabled for session %s",
                            self._session_id,
                        )
                    confirmed = data.get("confirmed", False)
                    logger.info(
                        "[confirm] Session %s request %s -> %s",
                        self._session_id,
                        request_id,
                        "confirmed" if confirmed else "denied",
                    )
                    return confirmed

            except Exception as exc:
                logger.debug("[confirm] Poll error: %s", exc)

        logger.warning(
            "[confirm] Timed out waiting for confirmation (session=%s, rid=%s) — denying",
            self._session_id,
            request_id,
        )
        return False  # timeout -> deny (fail-closed)

    async def _broadcast(self, msg: dict) -> None:
        try:
            await _get_async_http().post(
                f"/internal/sessions/{self._session_id}/broadcast",
                json=msg,
            )
        except Exception as exc:
            logger.warning("[confirm] Failed to broadcast %s: %s", msg.get("type"), exc)

    def set_sender(self, send_fn) -> None:  # noqa: ANN001
        """No-op — we use HTTP broadcast instead of the WS send_fn."""

    def resolve(self, *args, **kwargs) -> None:
        """No-op — responses arrive via HTTP polling, not broker.resolve()."""


# ---------------------------------------------------------------------------
# WorkerSecretProvider — sudo-password prompt over HTTP broadcast + poll.
# ---------------------------------------------------------------------------


class WorkerSecretProvider:
    """Sudo-password provider for the native local worker (split dispatch).

    ``shell()`` runs synchronously in a worker thread, so this is a SYNC
    provider (matching the ``shell.set_sudo_secret_provider`` contract:
    ``get(label) -> str | None`` and ``invalidate(label)``). It broadcasts a
    ``secret.request`` to the browser (POST /broadcast) and polls
    ``GET /internal/sessions/{id}/secret/{request_id}`` until the user answers,
    mirroring HttpConfirmationBroker. The value is memory-only and never logged.

    Caching caveat: this provider holds a sliding-TTL cache, but in split
    dispatch the worker builds a fresh instance per tool call (``execute_tool_saq``
    creates it and clears it in ``finally``), so the cache never spans calls — every
    ``sudo`` command re-prompts. Session-wide reuse only happens in in-process
    dispatch, where ``BrokerSecretProvider`` lives for the whole session. To get
    reuse here too, a session-scoped provider registry would be needed.
    """

    def __init__(self, session_id: str, ttl_seconds: int = 300) -> None:
        from agentforge.tools.sudo_cache import SudoCredentialCache

        self._session_id = session_id
        self._cache = SudoCredentialCache(ttl_seconds=ttl_seconds)

    def get(self, label: str) -> str | None:
        cached = self._cache.get(label)
        if cached is not None:
            return cached

        request_id = f"sr_{_uuid.uuid4().hex[:8]}"
        http = _get_sync_http()
        try:
            http.post(
                f"/internal/sessions/{self._session_id}/broadcast",
                json={
                    "type": "secret.request",
                    "request_id": request_id,
                    "label": label,
                    "prompt": "Enter sudo password",
                },
            )
        except Exception as exc:
            logger.warning("[secret] broadcast failed (session=%s): %s", self._session_id, exc)
            return None

        # Poll for the response. 290s < the shell tool's own deadline so this
        # returns explicitly before the tool times out.
        poll_url = f"/internal/sessions/{self._session_id}/secret/{request_id}"
        deadline = time.monotonic() + 290
        while time.monotonic() < deadline:
            time.sleep(0.5)
            try:
                resp = http.get(poll_url)
                if resp.status_code == 404:
                    continue
                data = resp.json()
                if data.get("ready"):
                    if data.get("cancelled"):
                        return None
                    value = data.get("value")
                    if not value:
                        return None
                    self._cache.set(label, value)
                    return value
            except Exception as exc:
                logger.debug("[secret] poll error: %s", exc)

        logger.warning(
            "[secret] timed out waiting for sudo password (session=%s) — failing closed",
            self._session_id,
        )
        return None

    def invalidate(self, label: str) -> None:
        self._cache.invalidate(label)


# ---------------------------------------------------------------------------
# _NullDatabase — stub ChatDatabase that proxies every write via HTTP.
# ---------------------------------------------------------------------------


class _NullDatabase:
    """Stub ChatDatabase for the worker process.

    The runners call db.add_message() inside send_and_persist. Rather than
    writing to SQLite directly (which would corrupt the DB when accessed from
    both a native macOS process and a Linux Docker container), add_message()
    POSTs each event to agentforge-web's internal HTTP endpoint so that agentforge-web
    — the sole owner of all SQLite files — handles the actual DB write and
    broadcasts to any live browser WebSocket connection.

    Other write methods (create/update_session) also proxy via HTTP.
    Read methods return minimal stubs so the runner code does not crash,
    except get_messages() which returns the pre-loaded conversation history
    so _build_conversation_history gives the agent its prior context.
    """

    def __init__(
        self,
        session_id: str,
        conversation_history: list[dict] | None = None,
        incognito_history: bool = False,
    ) -> None:
        self._session_id = session_id
        # Pre-loaded conversation history injected at enqueue time from
        # agentforge-web's real ChatDatabase. Returned by get_messages() so that
        # _build_conversation_history can reconstruct multi-turn context for
        # the runner without the worker ever touching SQLite directly.
        self._conversation_history: list[dict] = conversation_history or []
        # When True, message stubs are marked is_incognito so that
        # _build_conversation_history (incognito=True) keeps them instead
        # of filtering them out. Set for no_history agents.
        self._incognito_history = incognito_history

    def add_message(
        self,
        session_id: str,
        role: str = "assistant",
        msg_type: str | None = None,
        content: str | None = None,
        metadata: dict | None = None,
        tool_calls: list[dict] | None = None,
        is_incognito: bool = False,
    ) -> None:
        """Forward message persistence to agentforge-web via HTTP with the correct msg_type."""
        payload = {
            "msg": metadata or {},
            "role": role,
            "msg_type": msg_type,
            "content": content,
            "tool_calls": tool_calls,
            "is_incognito": is_incognito,
        }
        try:
            _get_sync_http().post(f"/internal/sessions/{session_id}/event", json=payload)
        except Exception as exc:
            logger.warning("Failed to persist message for session %s: %s", session_id, exc)

    def get_session(self, session_id: str):
        """Fetch real session data from agentforge-web via HTTP."""
        try:
            resp = _get_sync_http().get(f"/api/sessions/{session_id}", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return type(
                    "Session",
                    (),
                    {
                        "id": session_id,
                        "title": data.get("title", "New chat"),
                        "profile": data.get("profile"),
                        "model": data.get("model"),
                        "prompt_tokens": data.get("prompt_tokens", 0) or 0,
                        "completion_tokens": data.get("completion_tokens", 0) or 0,
                        "total_tokens": data.get("total_tokens", 0) or 0,
                        "to_dict": lambda self=None: data,
                    },
                )()
        except Exception:
            pass
        return type(
            "Session",
            (),
            {
                "id": session_id,
                "title": "New chat",
                "profile": None,
                "model": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "to_dict": lambda self=None: {},
            },
        )()

    def create_session(self, session_id: str, title: str = "New chat"):
        return type("Session", (), {"id": session_id, "title": title, "profile": None, "model": None})()

    def update_session(self, session_id: str, **kwargs):
        """Forward session updates (title, profile, model) to agentforge-web via HTTP."""
        if not kwargs:
            return
        payload = {k: str(v) if v is not None else None for k, v in kwargs.items()}
        try:
            _get_sync_http().post(f"/internal/sessions/{session_id}/update", json=payload)
        except Exception as exc:
            logger.warning("Failed to POST session update for %s: %s", session_id, exc)

    def add_token_usage(
        self,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
    ) -> None:
        """Forward token usage increment to agentforge-web via HTTP."""
        if prompt_tokens <= 0 and completion_tokens <= 0:
            return
        try:
            _get_sync_http().post(
                f"/internal/sessions/{session_id}/token-usage",
                json={"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
            )
        except Exception as exc:
            logger.warning("Failed to POST token usage for %s: %s", session_id, exc)

    def get_messages(self, session_id: str) -> list:
        """Return conversation history stubs for _build_conversation_history.

        Each stub mimics the ChatMessage attributes used by the function:
        .role and .content.  This gives the agent its multi-turn context
        so follow-up messages like "write a query for those tables" work.
        """
        return [
            type(
                "ChatMessage",
                (),
                {
                    "role": msg.get("role", ""),
                    "content": msg.get("content", "") or "",
                    "type": "result" if msg.get("role") == "assistant" else "query",
                    "is_incognito": self._incognito_history,
                    "metadata_json": None,
                    "tool_calls_json": None,
                },
            )()
            for msg in self._conversation_history
            if msg.get("role") in ("user", "assistant") and msg.get("content")
        ]

    def update_message_metadata(
        self,
        session_id: str,
        msg_type: str,
        metadata_patch: dict,
    ) -> bool:
        """Proxy metadata update to agentforge-web via HTTP."""
        try:
            resp = _get_sync_http().post(
                f"/internal/sessions/{session_id}/messages/{msg_type}/metadata",
                json=metadata_patch,
                timeout=5,
            )
            return resp.status_code == 200
        except Exception as exc:
            logger.warning("Failed to update message metadata for %s: %s", session_id, exc)
            return False

    def get_statistics(self) -> dict:
        return {}


# ---------------------------------------------------------------------------
# SaqCancelEvent — async-friendly cancel signal for SAQ jobs.
# ---------------------------------------------------------------------------


class SaqCancelEvent:
    """Cancel event for SAQ agent runs.

    Three signals, in priority order:

    1. Local flag (``.set()`` called in-process).
    2. SAQ native abort — the Job in Redis has its status flipped to
       ``Status.ABORTING`` when agentforge-web calls ``Job.fetch(queue, key).abort()``
       (or equivalently ``queue.abort(job, error)``). We refetch the job from
       the queue every ``saq_poll_interval`` seconds and check.
    3. HTTP fallback — polls ``/internal/jobs/{id}/cancelled`` every
       ``http_poll_interval`` seconds. Covers the case where the browser Stop
       button hits agentforge-web's job_store before the SAQ abort propagates.

    Always safe to call from the event loop. ``is_set()`` is a cheap in-memory
    flag check; the SAQ and HTTP polls run as a single async background task
    that never blocks.

    Usage::

        cancel_event = SaqCancelEvent(ctx, job_id)
        await cancel_event.start()
        try:
            ...
            if cancel_event.is_set():
                break
        finally:
            await cancel_event.stop()
    """

    def __init__(
        self,
        ctx: dict,
        job_id: str,
        *,
        saq_poll_interval: float = 1.0,
        http_poll_interval: float = 3.0,
        max_poll_seconds: float = 1200.0,
    ) -> None:
        self._ctx = ctx
        self._job_id = job_id
        self._saq_poll_interval = saq_poll_interval
        self._http_poll_interval = http_poll_interval
        # Safety net: if stop() is never called (leak) and the job never reaches
        # a terminal state we can detect, give up after this many seconds. Must
        # be > _SAQ_AGENT_TIMEOUT (900s) so normal long-running jobs aren't cut
        # short while leaks still self-clean.
        self._max_poll_seconds = max_poll_seconds
        self._flag = False
        self._poll_task: asyncio.Task | None = None

    def is_set(self) -> bool:
        return self._flag

    def set(self) -> None:
        self._flag = True

    async def start(self) -> None:
        """Start the background polling task (SAQ + HTTP)."""
        if self._poll_task is None:
            self._poll_task = asyncio.create_task(
                self._poll_loop(),
                name=f"saq-cancel-poll-{self._job_id[:8]}",
            )

    async def stop(self) -> None:
        """Cancel the polling task. Safe to call multiple times."""
        if self._poll_task is None:
            return
        self._poll_task.cancel()
        try:
            await self._poll_task
        except asyncio.CancelledError:
            pass
        self._poll_task = None

    async def _poll_loop(self) -> None:
        """Poll SAQ + agentforge-web until cancelled, terminal, or max age.

        Exit conditions (in precedence):
        - `stop()` cancels the task (normal path on job completion)
        - Abort detected (SAQ status ABORTING or agentforge-web cancelled=true) →
          set flag so callers see is_set()=True
        - Job reached terminal state (SAQ complete/failed/aborted, agentforge-web
          done=true) → exit quietly, flag stays False
        - Max age exceeded → warn and exit (leak safety net)
        """
        next_http = time.monotonic() + self._http_poll_interval
        deadline = time.monotonic() + self._max_poll_seconds
        while not self._flag:
            try:
                await asyncio.sleep(self._saq_poll_interval)

                if time.monotonic() >= deadline:
                    logger.warning(
                        "SaqCancelEvent: poll loop exceeded %.0fs for %s — terminating (possible leak)",
                        self._max_poll_seconds,
                        self._job_id,
                    )
                    return

                saq_status = await self._saq_status()
                if saq_status == "aborting":
                    logger.info("SaqCancelEvent: SAQ ABORTING for %s", self._job_id)
                    self._flag = True
                    return
                if saq_status in {"complete", "failed", "aborted"}:
                    logger.debug(
                        "SaqCancelEvent: SAQ terminal=%s for %s — stopping poll",
                        saq_status,
                        self._job_id,
                    )
                    return

                now = time.monotonic()
                if now >= next_http:
                    next_http = now + self._http_poll_interval
                    state = await self._http_state()
                    if state.get("cancelled"):
                        logger.info("SaqCancelEvent: HTTP cancelled=true for %s", self._job_id)
                        self._flag = True
                        return
                    if state.get("done"):
                        logger.debug(
                            "SaqCancelEvent: HTTP done=true for %s — stopping poll",
                            self._job_id,
                        )
                        return

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("SaqCancelEvent poll error: %s", exc)

    async def _saq_status(self) -> str | None:
        """Return the current SAQ job status as a lowercase string, or None.

        Known values: queued, active, aborting, aborted, complete, failed.
        """
        job = self._ctx.get("job")
        if job is None:
            return None
        try:
            queue = job.queue
            fresh = await queue.job(job.key)
            if fresh is None:
                return None
            status = getattr(fresh, "status", None)
            if status is None:
                return None
            value = getattr(status, "value", status)
            return str(value).lower()
        except Exception as exc:
            logger.debug("SaqCancelEvent: job.refresh failed: %s", exc)
            return None

    async def _http_state(self) -> dict:
        """Async HTTP poll of agentforge-web /internal/jobs/{id}/cancelled.

        Returns the raw response dict (keys: cancelled, done). On error
        returns an empty dict so callers treat it as "state unknown".
        """
        try:
            client = _get_async_http()
            resp = await client.get(f"/internal/jobs/{self._job_id}/cancelled")
            data = resp.json()
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}
