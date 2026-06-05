"""Secret broker — bridges the sync agent thread to the client for a masked
password prompt. Parallels confirm.py, but returns a STRING and has NO
auto-accept path. The value is memory-only; callers must never persist/log it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from agentforge.tools.sudo_cache import SudoCredentialCache

from . import protocol


@dataclass
class _Pending:
    request_id: str
    future: asyncio.Future


class SecretBroker:
    """Per-session secret prompts. NO auto-accept."""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._send: Callable[[dict], None] | None = None

    def set_sender(self, send_fn: Callable[[dict], None]) -> None:
        self._send = send_fn

    async def request(self, label: str, prompt: str) -> str | None:
        request_id = f"sr_{uuid4().hex[:8]}"
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = _Pending(request_id, future)
        if self._send:
            self._send(protocol.secret_request(request_id, label, prompt))
        try:
            return await asyncio.wait_for(future, timeout=300)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, request_id: str, *, value: str | None = None, cancelled: bool = False) -> bool:
        """Resolve a pending in-process request. Returns True if one was waiting.

        False means no in-process waiter owned this request_id — i.e. it came
        from the split-dispatch worker, which polls for the answer separately.
        """
        pending = self._pending.get(request_id)
        if pending and not pending.future.done():
            pending.future.set_result(None if cancelled else value)
            return True
        return False


class BrokerSecretProvider:
    """Sync provider for shell.set_sudo_secret_provider(); bridges to the async
    broker via run_coroutine_threadsafe.

    In-process dispatch only. The AgentBridge builds one of these per session and
    keeps it for the session's lifetime, so its sliding-TTL cache genuinely spans
    tool calls: a repeat sudo within the TTL window reuses the entered password and
    doesn't re-prompt. (The split-dispatch worker path is different — see
    WorkerSecretProvider, which re-prompts on every command.)
    """

    def __init__(self, broker: "SecretBroker", loop: asyncio.AbstractEventLoop, ttl_seconds: int = 300) -> None:
        self._broker = broker
        self._loop = loop
        self._cache = SudoCredentialCache(ttl_seconds=ttl_seconds)

    def get(self, label: str) -> str | None:
        cached = self._cache.get(label)
        if cached is not None:
            return cached
        prompt = "Enter sudo password"
        fut = asyncio.run_coroutine_threadsafe(self._broker.request(label, prompt), self._loop)
        try:
            secret = fut.result(timeout=300)
        except Exception:
            return None
        if not secret:
            return None
        self._cache.set(label, secret)
        return secret

    def invalidate(self, label: str) -> None:
        self._cache.invalidate(label)

    def clear(self) -> None:
        self._cache.clear()
