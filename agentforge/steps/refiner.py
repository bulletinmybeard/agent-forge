"""InputRefiner — rewrite the user query to be more specific and actionable."""

from __future__ import annotations

from collections.abc import Callable

from chalkbox.logging.bridge import get_logger

from ..client import AIClient
from ..context import PipelineContext
from .base import BaseStep

logger = get_logger(__name__)

DEFAULT_REFINE_PROMPT = (
    "You are an input refiner. Rewrite the user's query to be more specific, "
    "clear, and actionable. Keep the same intent. Reply with ONLY the refined query, "
    "nothing else."
)


class InputRefiner(BaseStep):
    """Rewrite the user query for clarity before the main LLM step.

    The refined query replaces the last user message in ``ctx.messages``
    and is also stored in ``ctx.metadata["original_query"]`` (the original).
    """

    def __init__(
        self,
        client: AIClient,
        *,
        system_prompt: str = DEFAULT_REFINE_PROMPT,
        name: str = "InputRefiner",
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name, condition=condition)
        self._client = client
        self._system_prompt = system_prompt

    def process(self, ctx: PipelineContext) -> PipelineContext:
        # Save original
        ctx.metadata["original_query"] = ctx.query

        # Ask the model to refine
        refine_messages = [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": ctx.query},
        ]
        response = self._client.chat(refine_messages)
        refined = response.content.strip()

        if refined:
            logger.info("Refined query: %s → %s", ctx.query[:80], refined[:80])
            ctx.query = refined

            # Replace the last user message with the refined version
            for msg in reversed(ctx.messages):
                if msg["role"] == "user":
                    msg["content"] = refined
                    break
        else:
            logger.warning("Refiner returned empty — keeping original query")

        return ctx
