"""Backend protocol — provider-neutral interface that AIClient delegates to.

Every backend takes an AIProfile at construction time and exposes the two
operations AIClient needs: non-streaming chat (sync + async) and streaming
chat (sync + async generators).

All inputs and outputs use the canonical "Ollama-shaped" wire format that
the framework's agent loop, router, and tools already speak:

- Messages are dicts with ``role`` (system/user/assistant/tool) and ``content``.
  Optional ``tool_calls`` on assistant messages carry prior tool invocations.
  Optional ``images`` on user messages carry raw image bytes.
- Tools are plain Python callables; backends convert them to the provider's
  tool spec format at the boundary.
- Responses come back as a :class:`agentforge.client.ChatResponse` — content
  string, optional thinking trace, optional tool_calls list, provider-agnostic.

Backend authors translate in and out of this shape. The public AIClient API
never changes based on which backend is in use.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncGenerator, Callable, Generator
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..client import ChatResponse
    from ..config import AIProfile


class Backend(ABC):
    """Abstract base for provider backends."""

    def __init__(self, profile: AIProfile) -> None:
        self._profile = profile

    @property
    def profile(self) -> AIProfile:
        return self._profile

    @abstractmethod
    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
    ) -> ChatResponse:
        """Synchronous non-streaming chat. Returns a normalised ChatResponse."""

    @abstractmethod
    async def achat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
    ) -> ChatResponse:
        """Asynchronous non-streaming chat. Returns a normalised ChatResponse."""

    @abstractmethod
    def stream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
    ) -> Generator[dict, None, None]:
        """Synchronous streaming chat.

        Yields dicts of the shape ``{"content": str, "done": bool, "raw": Any}``.
        Content chunks may be empty strings; the final chunk has ``done=True``.
        """

    @abstractmethod
    def astream(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[Callable] | None = None,
        temperature: float | None = None,
        deep_think: bool = False,
        keep_alive: bool | None = None,
    ) -> AsyncGenerator[dict, None]:
        """Asynchronous streaming chat. Same shape as :meth:`stream`."""
