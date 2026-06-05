"""Pipeline — runs an ordered list of steps over a shared context."""

from __future__ import annotations

import time
from typing import Any

from chalkbox.logging.bridge import get_logger

from .context import PipelineContext
from .steps.base import BaseStep

logger = get_logger(__name__)


class Pipeline:
    """Execute a sequence of :class:``BaseStep`` instances over a :class:``PipelineContext``.

    Usage::

        pipeline = Pipeline([
            InputRefiner(profile="refiner"),
            Thinker(profile="thinker", condition=lambda ctx: ctx.metadata.get("complex")),
            OutputRefiner(profile="refiner"),
        ])
        ctx = PipelineContext(query="How do I reset my password?")
        result = pipeline.run(ctx)
        print(result.result)
    """

    def __init__(
        self,
        steps: list[BaseStep] | None = None,
        *,
        name: str = "Pipeline",
        verbose: bool = False,
    ) -> None:
        self.steps: list[BaseStep] = steps or []
        self.name = name
        self.verbose = verbose

    # -- builder pattern (optional) -----------------------------------------

    def add_step(self, step: BaseStep) -> "Pipeline":
        """Append a step and return *self* (for chaining)."""
        self.steps.append(step)
        return self

    # -- execution ----------------------------------------------------------

    def run(self, ctx: PipelineContext | None = None, **kwargs: Any) -> PipelineContext:
        """Run the full pipeline sequentially."""
        if ctx is None:
            ctx = PipelineContext(**kwargs)

        # Seed the messages list with the user query if it hasn't been done yet
        if ctx.query and not ctx.messages:
            ctx.add_user_message(ctx.query)

        logger.info("[%s] Starting with %d step(s)", self.name, len(self.steps))
        pipeline_start = time.perf_counter()

        for i, step in enumerate(self.steps, 1):
            if not step.should_run(ctx):
                logger.info("[%s] Step %d/%d SKIPPED: %s", self.name, i, len(self.steps), step.name)
                continue

            logger.info("[%s] Step %d/%d: %s", self.name, i, len(self.steps), step.name)
            step_start = time.perf_counter()

            try:
                ctx = step.process(ctx)
            except Exception as exc:
                ctx.add_error(f"Step '{step.name}' failed: {exc}")
                logger.exception("Step '%s' raised an exception", step.name)
                # Continue to next step — the pipeline is resilient by default.
                # Steps that must abort the pipeline should re-raise or use a custom flag.
                continue

            elapsed = time.perf_counter() - step_start
            if self.verbose:
                preview = (ctx.result[:120] + "...") if len(ctx.result) > 120 else ctx.result
                logger.info(
                    "[%s] Step %s completed in %.2fs — result preview: %s",
                    self.name,
                    step.name,
                    elapsed,
                    preview or "(empty)",
                )
            else:
                logger.debug("[%s] Step %s completed in %.2fs", self.name, step.name, elapsed)

        total = time.perf_counter() - pipeline_start
        logger.info("[%s] Finished in %.2fs — %d error(s)", self.name, total, len(ctx.errors))

        return ctx

    def __repr__(self) -> str:
        step_names = ", ".join(s.name for s in self.steps)
        return f"<Pipeline name={self.name!r} steps=[{step_names}]>"
