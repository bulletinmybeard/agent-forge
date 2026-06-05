"""LLMStep — send the current messages to the model and store the response."""

from __future__ import annotations

from collections.abc import Callable

from chalkbox.logging.bridge import get_logger

from ..client import AIClient
from ..context import PipelineContext
from ..tools import ToolRegistry
from .base import BaseStep

logger = get_logger(__name__)


class LLMStep(BaseStep):
    """Chat with the model using the messages accumulated in the context.

    The assistant's reply is appended to ``ctx.messages`` and also written
    to ``ctx.result``.  If ``deep_think`` is enabled, ``ctx.thinking`` is
    populated with the chain-of-thought content.

    Optionally accepts a ``ToolRegistry`` — when provided, the registered
    tools are passed to the model so it can choose to call them.  Any
    returned tool calls are stored in ``ctx.tool_calls`` for a subsequent
    ToolExecutor step.
    """

    def __init__(
        self,
        client: AIClient,
        *,
        system_prompt: str | None = None,
        registry: ToolRegistry | None = None,
        tools: list[str] | None = None,
        deep_think: bool = False,
        temperature: float | None = None,
        name: str | None = None,
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name or "LLMStep", condition=condition)
        self._client = client
        self._system_prompt = system_prompt
        self._registry = registry
        self._tool_names = tools
        self._deep_think = deep_think
        self._temperature = temperature

    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Optionally inject/replace system prompt
        if self._system_prompt:
            ctx.add_system_message(self._system_prompt)

        # Ollama requires the last message to be user or tool — not assistant.
        # When a previous step (e.g., AgentLoopStep) ended with an assistant
        # message, bridge the gap with a user prompt so the model can continue.
        if ctx.messages and ctx.messages[-1].get("role") == "assistant":
            bridge = ctx.query or "Please continue."
            if self._system_prompt:
                bridge = "Based on the conversation above, follow the system instructions."
            ctx.add_user_message(bridge)

        # Resolve tools to pass to the model
        tool_callables: list[Callable] | None = None
        if self._registry and len(self._registry) > 0:
            tool_callables = self._registry.as_callables(self._tool_names)

        response = self._client.chat(
            ctx.messages,
            attachments=ctx.attachments or None,
            tools=tool_callables,
            deep_think=self._deep_think,
            temperature=self._temperature,
        )

        ctx.result = response.content
        ctx.add_assistant_message(response.content)

        if response.thinking:
            ctx.thinking = response.thinking

        if response.tool_calls:
            ctx.tool_calls = response.tool_calls

        logger.debug("LLMStep produced %d chars", len(ctx.result))
        return ctx
