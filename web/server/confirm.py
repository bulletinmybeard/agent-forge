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


@dataclass
class PendingConfirmation:
    request_id: str
    prompt: str
    future: asyncio.Future


class ConfirmationBroker:
    """Manages pending confirmation requests for a single WebSocket session."""

    def __init__(self) -> None:
        self._pending: dict[str, PendingConfirmation] = {}
        self._send: Callable[[dict], None] | None = None
        # Set to True when the user clicks "Yes (all)" — subsequent prompts are
        # auto-accepted for the remainder of the current agent run.
        # Reset to False at the start of each new user message.
        self.auto_accept: bool = False

    def set_sender(self, send_fn: Callable[[dict], None]) -> None:
        """Set the function used to send messages to the client."""
        self._send = send_fn

    async def request(self, prompt: str) -> bool:
        """Send a confirmation request and wait for the client's answer.

        If ``auto_accept`` is True (user clicked "Yes (all)" earlier in this
        run), returns True immediately and sends a silent auto-accepted
        notification to the client instead of blocking for a response.

        Called from the agent thread via ``run_coroutine_threadsafe``.
        """
        if self.auto_accept:
            # Notify the UI so it shows the auto-accepted confirmation inline,
            # but don't block waiting for a response.
            if self._send:
                self._send(protocol.confirm_request(f"cr_{uuid4().hex[:8]}", prompt, auto_accepted=True))
            return True

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
            self._send(protocol.confirm_request(request_id, prompt))

        # Wait for the client to respond
        try:
            confirmed = await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            confirmed = False
        finally:
            self._pending.pop(request_id, None)

        return confirmed

    def resolve(self, request_id: str, confirmed: bool) -> None:
        """Resolve a pending confirmation — called when client sends response."""
        pending = self._pending.get(request_id)
        if pending and not pending.future.done():
            pending.future.set_result(confirmed)


def make_sync_confirm_handler(
    broker: ConfirmationBroker,
    loop: asyncio.AbstractEventLoop,
) -> Callable[[str], bool]:
    """Create a synchronous confirm handler that bridges to the async broker.

    This is passed to ``registry.set_confirm_handler()`` so the sync agent
    thread can request confirmation from the async WebSocket client.
    """

    def handler(prompt: str) -> bool:
        future = asyncio.run_coroutine_threadsafe(broker.request(prompt), loop)
        return future.result(timeout=300)

    return handler
