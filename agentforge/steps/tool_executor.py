"""ToolExecutor — run the tool calls stored in ctx.tool_calls."""

from __future__ import annotations

from collections.abc import Callable

from chalkbox.logging.bridge import get_logger

from ..context import PipelineContext
from ..tools import ToolRegistry
from .base import BaseStep

logger = get_logger(__name__)


class ToolExecutor(BaseStep):
    """Execute tool calls that were detected by a previous step.

    Reads ``ctx.tool_calls``, executes each one via the registry, and stores
    the results in ``ctx.tool_results``.  A tool message is also appended to
    ``ctx.messages`` so that a subsequent LLM step can incorporate the results.

    If ``ctx.tool_calls`` is empty or *None*, this step is a no-op.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "ToolExecutor",
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name, condition=condition)
        self._registry = registry

    def process(self, ctx: PipelineContext) -> PipelineContext:
        if not ctx.tool_calls:
            logger.debug("ToolExecutor: no tool calls — skipping")
            return ctx

        results: list[dict] = []

        for tc in ctx.tool_calls:
            name = tc["name"]
            args = tc.get("arguments", {})

            try:
                output = self._registry.execute(name, args)
                result_str = str(output)
            except KeyError:
                result_str = f"Error: unknown tool '{name}'"
                ctx.add_error(result_str)
            except Exception as exc:
                result_str = f"Error executing '{name}': {exc}"
                ctx.add_error(result_str)

            results.append(
                {
                    "name": name,
                    "arguments": args,
                    "result": result_str,
                }
            )

            # Add tool result as a message so the next LLM step sees it
            ctx.messages.append(
                {
                    "role": "tool",
                    "content": result_str,
                }
            )

            logger.info("Tool '%s' → %s", name, result_str[:120])

        ctx.tool_results = results
        return ctx
