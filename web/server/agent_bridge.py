"""Agent bridge — connects AgentLoop callbacks to WebSocket event senders.

Replaces ``UI.*`` calls from interactive chat with WebSocket messages.
Each callback sends a JSON event to the connected client.

Ported from py-mini-ai-framework for hybrid search+agent mode.

Usage::

    bridge = AgentBridge(send_fn, broker, loop)
    bridge.setup_registry(registry)
    # Now when the agent executes tools, events are sent over WebSocket.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from agentforge.tools.shell import set_sudo_secret_provider

from . import protocol
from .confirm import ConfirmationBroker, make_sync_confirm_handler
from .secret import BrokerSecretProvider, SecretBroker


class AgentBridge:
    """Bridges the sync agent world to the async WebSocket world."""

    def __init__(
        self,
        send_fn: Callable[[dict], None],
        broker: ConfirmationBroker,
        loop: asyncio.AbstractEventLoop,
        *,
        secret_broker: "SecretBroker | None" = None,
        db: Any | None = None,
        session_id: str = "",
        incognito: bool = False,
    ) -> None:
        self._send = send_fn
        self._broker = broker
        self._secret_broker = secret_broker
        self._loop = loop
        self._secret_provider: BrokerSecretProvider | None = None
        # For persisting file.diff cards (diff-preview confirm). The client
        # renders file.diff from persisted message rows, not raw broadcasts, so
        # the preview card must be written to the DB to show up + survive reload.
        self._db = db
        self._session_id = session_id
        self._incognito = incognito

    def setup_registry(self, registry: Any) -> None:
        """Wire registry callbacks to send WebSocket events.

        Uses indirect lambdas so that callers can replace
        ``self._on_tool_call`` / ``self._on_tools_complete`` after
        ``setup_registry`` and the registry will still call the
        replacement (e.g., persisting wrappers).
        """
        registry.set_tool_call_handler(lambda *a, **kw: self._on_tool_call(*a, **kw))
        registry.set_tools_complete_handler(lambda *a, **kw: self._on_tools_complete(*a, **kw))
        registry.set_confirm_handler(make_sync_confirm_handler(self._broker, self._loop))
        registry.set_file_diff_handler(self._on_file_diff)

        # Secret provider — mirrors the confirm broker wiring. The broker itself is
        # created + sender-wired in ws_endpoint (so the WS receive loop can resolve
        # it) and passed in. Only wire the provider when a broker was supplied.
        if self._secret_broker is not None:
            self._secret_provider = BrokerSecretProvider(self._secret_broker, self._loop)
            set_sudo_secret_provider(self._secret_provider)

    def close(self) -> None:
        """Clear per-run state. Call after each agent run to reset the provider."""
        if self._secret_provider is not None:
            self._secret_provider.clear()
        set_sudo_secret_provider(None)

    def _on_file_diff(self, payload: dict[str, Any]) -> None:
        msg = protocol.file_diff(**payload)
        self._send(msg)  # live broadcast
        if self._db is not None and self._session_id:
            try:
                self._db.add_message(
                    session_id=self._session_id,
                    role="assistant",
                    msg_type="file_diff",
                    metadata=msg,
                    is_incognito=self._incognito,
                )
            except Exception:
                pass  # persistence errors must never break execution

    # -- Callbacks (called from agent thread) --------------------------------

    def _on_tool_call(self, name: str, args: dict[str, Any], guard: dict | None = None) -> None:
        self._send(protocol.tool_call(name, args, guard))

    def _on_tools_complete(self) -> None:
        self._send(protocol.tool_calls_flush())

    # -- High-level run helpers ---------------------------------------------

    def send_routing(self) -> None:
        self._send(protocol.agent_routing())

    def send_routed(self, profile: str, reason: str, elapsed: float) -> None:
        self._send(protocol.agent_routed(profile, reason, elapsed))

    def send_config(
        self,
        profile: str,
        model: str,
        tools: int,
        session_id: str,
    ) -> None:
        self._send(protocol.agent_config(profile, model, tools, session_id))

    def send_result(self, text: str, elapsed: float) -> None:
        self._send(protocol.agent_result(text, elapsed))

    def send_summary(
        self,
        iterations: int,
        elapsed: float,
        tool_calls: int,
        tools: dict[str, int],
    ) -> None:
        self._send(protocol.agent_summary(iterations, elapsed, tool_calls, tools))

    def send_error(self, message: str, recoverable: bool = False) -> None:
        self._send(protocol.agent_error(message, recoverable))
