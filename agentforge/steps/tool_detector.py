"""ToolDetector — send messages + tool specs to the model to get tool_calls back."""

from __future__ import annotations

from collections.abc import Callable

from chalkbox.logging.bridge import get_logger

from ..client import AIClient
from ..context import PipelineContext
from ..tools import ToolRegistry
from .base import BaseStep

logger = get_logger(__name__)


class ToolDetector(BaseStep):
    """Ask the model which tools (if any) to call for the current query.

    The model receives the conversation messages together with Ollama tool
    specifications generated from the registry.  If the model responds with
    tool calls, they are stored in ``ctx.tool_calls``.  If the model responds
    with plain text instead (no tools needed), that text is stored in
    ``ctx.result`` so downstream steps can skip execution.
    """

    def __init__(
        self,
        client: AIClient,
        registry: ToolRegistry,
        *,
        tools: list[str] | None = None,
        name: str = "ToolDetector",
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name, condition=condition)
        self._client = client
        self._registry = registry
        self._tool_names = tools

    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Get the callable list to pass to Ollama
        callables = self._registry.as_callables(self._tool_names)
        if not callables:
            logger.warning("ToolDetector: no tools available — skipping")
            return ctx

        response = self._client.chat(
            ctx.messages,
            attachments=ctx.attachments or None,
            tools=callables,
        )

        if response.tool_calls:
            ctx.tool_calls = response.tool_calls

            # Add the assistant's tool-call message to the conversation so that
            # subsequent ``tool`` role messages are valid (Ollama requires
            # user → assistant(tool_calls) → tool ordering).
            ctx.messages.append(
                {
                    "role": "assistant",
                    "content": response.content or "",
                    "tool_calls": [
                        {
                            "function": {"name": tc["name"], "arguments": tc["arguments"]},
                        }
                        for tc in response.tool_calls
                    ],
                }
            )

            logger.info(
                "ToolDetector found %d tool call(s): %s",
                len(response.tool_calls),
                [tc["name"] for tc in response.tool_calls],
            )
        else:
            # Model chose not to use tools — store its text response
            ctx.result = response.content
            ctx.add_assistant_message(response.content)
            logger.info("ToolDetector: model responded without tool calls")

        if response.thinking:
            ctx.thinking = response.thinking

        return ctx
