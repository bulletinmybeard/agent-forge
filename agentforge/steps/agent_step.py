"""AgentLoopStep — embed an AgentLoop as a pipeline step."""

from __future__ import annotations

from collections.abc import Callable

from chalkbox.logging.bridge import get_logger

from ..agent import AgentLoop
from ..client import AIClient
from ..context import PipelineContext
from ..tools import ToolRegistry
from .base import BaseStep

logger = get_logger(__name__)


class AgentLoopStep(BaseStep):
    """Run an :class:`AgentLoop` as a pipeline step.

    This allows you to combine linear pipeline steps with an iterative agent
    loop in the middle.  For example::

        pipeline = Pipeline([
            InputRefiner(refiner_client),
            AgentLoopStep(main_client, registry, max_iterations=5),
            OutputRefiner(main_client),
        ])

    The agent receives the current ``ctx`` (including messages, attachments,
    and metadata from prior steps) and runs its think/act/observe loop.
    When finished, the updated context flows to the next pipeline step.
    """

    def __init__(
        self,
        client: AIClient,
        registry: ToolRegistry,
        *,
        system_prompt: str | None = None,
        tools: list[str] | None = None,
        max_iterations: int = 10,
        deep_think: bool = False,
        temperature: float | None = None,
        verbose: bool = False,
        name: str = "AgentLoopStep",
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        super().__init__(name=name, condition=condition)

        kwargs: dict = {
            "tools": tools,
            "max_iterations": max_iterations,
            "deep_think": deep_think,
            "temperature": temperature,
            "verbose": verbose,
        }
        if system_prompt is not None:
            kwargs["system_prompt"] = system_prompt

        self._agent = AgentLoop(client, registry, **kwargs)

    def process(self, ctx: PipelineContext) -> PipelineContext:
        return self._agent.run(ctx=ctx)
