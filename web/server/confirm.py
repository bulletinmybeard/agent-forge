"""Confirmation broker — bridges sync agent ↔ async WebSocket for user prompts.

When a destructive tool (e.g., delete_file) fires, the ToolRegistry calls the
confirm handler synchronously.  This module provides an async broker that:

1. Sends a ``confirm.request`` to the client via WebSocket.
2. Awaits the client's ``confirm.response`` via an ``asyncio.Future``.
3. Returns the boolean result back to the sync caller.

The sync ↔ async bridge uses ``asyncio.run_coroutine_threadsafe`` since the
agent loop runs in a separate thread.

Ported from py-mini-ai-framework for hybrid search+agent mode.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from . import protocol

# Shorter than make_sync_confirm_handler's outer join so the broker always
# returns a ConfirmDecision instead of the handler hitting TimeoutError.
CONFIRM_TIMEOUT_SECONDS = 290.0


@dataclass(frozen=True)
class ConfirmDecision:
    """Result of a confirm prompt. Bool-false means do not proceed."""

    confirmed: bool
    timed_out: bool = False
    auto_accepted: bool = False

    def __bool__(self) -> bool:
        return self.confirmed


@dataclass
class PendingConfirmation:
    request_id: str
    prompt: str
    future: asyncio.Future


def unanswered_confirm_request(messages: list) -> dict | None:
    """Latest ``confirm.request`` that has no matching answer.

    Used on WS reconnect while a job is still waiting on Yes/No. Confirms are
    not replayed from the Redis buffer (they would re-open answered dialogs),
    so without this a reload leaves the run blocked with a blank UI.
    """
    pending: dict[str, dict] = {}
    answered: set[str] = set()
    for msg in messages:
        msg_type = getattr(msg, "type", None) or (msg.get("type") if isinstance(msg, dict) else None)
        meta = getattr(msg, "metadata", None)
        if meta is None and isinstance(msg, dict):
            meta = msg.get("metadata") or msg
        if not isinstance(meta, dict):
            continue
        rid = str(meta.get("request_id") or "")
        if not rid:
            continue
        if msg_type == "confirm_prompt" or meta.get("type") == "confirm.request":
            pending[rid] = {
                "type": "confirm.request",
                "request_id": rid,
                "prompt": meta.get("prompt") or getattr(msg, "content", None) or "",
            }
        elif msg_type in ("confirm_answer",) or meta.get("type") in (
            "confirm.response",
            "confirm.timeout",
            "confirm_answer",
        ):
            answered.add(rid)
    for rid in reversed(list(pending)):
        if rid not in answered:
            return pending[rid]
    return None


class ConfirmationBroker:
    """Manages pending confirmation requests for a single WebSocket session."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._send: Callable[[dict], None] | None = None
        # Set to True when the user clicks "This session" — subsequent prompts
        # in this chat are auto-accepted. Also seeded from state.session_auto_accept.
        self.auto_accept: bool = False
        self.session_id: str | None = None

    def set_sender(self, send_fn: Callable[[dict], None]) -> None:
        """Set the function used to send messages to the client."""
        self._send = send_fn

    async def request(
        self,
        prompt: str,
        *,
        ignore_auto_accept: bool = False,
        kind: str | None = None,
    ) -> ConfirmDecision:
        """Send a confirmation request and wait for the client's answer.

        If ``auto_accept`` is True (user clicked "This session" earlier in this
        run), returns confirmed immediately and sends a silent auto-accepted
        notification to the client instead of blocking for a response.

        ``ignore_auto_accept`` is for gates that must always wait (plan approve).
        ``kind`` is forwarded to the UI (e.g. ``plan`` hides This-session).

        Called from the agent thread via ``run_coroutine_threadsafe``.
        """
        if not ignore_auto_accept:
            if not self.auto_accept and self.session_id:
                from . import state as _state

                self.auto_accept = self.session_id in _state.session_auto_accept
            if self.auto_accept:
                # Notify the UI so it shows the auto-accepted confirmation inline,
                # but don't block waiting for a response.
                if self._send:
                    self._send(
                        protocol.confirm_request(
                            f"cr_{uuid4().hex[:8]}",
                            prompt,
                            auto_accepted=True,
                            kind=kind,
                        )
                    )
                return ConfirmDecision(confirmed=True, auto_accepted=True)

        request_id = f"cr_{uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        future: asyncio.Future[bool] = loop.create_future()

        self._pending[request_id] = PendingConfirmation(
            request_id=request_id,
            prompt=prompt,
            future=future,
        )

        # Send the request to the client
        if self._send:
            self._send(protocol.confirm_request(request_id, prompt, kind=kind))

        timed_out = False
        try:
            confirmed = await asyncio.wait_for(future, timeout=CONFIRM_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            confirmed = False
            timed_out = True
            if self._send:
                self._send(protocol.confirm_timeout(request_id))
        finally:
            self._pending.pop(request_id, None)

        return ConfirmDecision(confirmed=confirmed, timed_out=timed_out)

    def resolve(self, request_id: str, confirmed: bool) -> None:
        """Resolve a pending confirmation — called when client sends response."""
        pending = self._pending.get(request_id)
        if pending and not pending.future.done():
            pending.future.set_result(confirmed)


def make_sync_confirm_handler(
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[str], ConfirmDecision]:
    """Create a synchronous confirm handler that bridges to the async broker.

    This is passed to ``registry.set_confirm_handler()`` so the sync agent
    thread can request confirmation from the async WebSocket client.
    """

    def handler(prompt: str) -> ConfirmDecision:
        future = asyncio.run_coroutine_threadsafe(broker.request(prompt), loop)
        try:
            # Outer join is longer than CONFIRM_TIMEOUT_SECONDS so the broker
            # always returns a ConfirmDecision instead of this raising.
            result = future.result(timeout=CONFIRM_TIMEOUT_SECONDS + 15)
        except Exception:
            return ConfirmDecision(confirmed=False, timed_out=True)
        if isinstance(result, ConfirmDecision):
            return result
        return ConfirmDecision(confirmed=bool(result))

    return handler
