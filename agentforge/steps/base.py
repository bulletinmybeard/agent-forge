"""BaseStep — abstract base class for all pipeline steps."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable

from ..context import PipelineContext


class BaseStep(ABC):
    """A single unit of work in a pipeline.

    Subclasses **must** implement :meth:`process`.  They may optionally override
    :attr:`name` and implement a ``condition`` callable that decides whether the
    step should run for a given context.
    """

    def __init__(
        self,
        *,
        name: str | None = None,
        condition: Callable[[PipelineContext], bool] | None = None,
    ) -> None:
        self.name: str = name or self.__class__.__name__
        self.condition = condition

    @abstractmethod
    def process(self, ctx: PipelineContext) -> PipelineContext:
        """Execute this step's logic.

        Must return the (possibly mutated) *ctx* to pass to the next step.
        """
        ...

    def should_run(self, ctx: PipelineContext) -> bool:
        """Return *True* if this step should execute for the given context."""
        if self.condition is not None:
            return self.condition(ctx)
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} name={self.name!r}>"
