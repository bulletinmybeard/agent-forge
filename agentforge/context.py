"""PipelineContext — the shared state bag that flows through every pipeline step."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from chalkbox.logging.bridge import get_logger

if TYPE_CHECKING:
    from .attachments import Attachment

logger = get_logger(__name__)


@dataclass
class PipelineContext:
    """Mutable state carried through a pipeline run.

    Every step reads from and writes to this object.
    Steps can store arbitrary data in ``metadata`` for downstream steps to consume.

    Attributes:
        query:          the original user query / prompt.
        messages:       the conversation so far (system + user + assistant turns).
        result:         the final output produced by the pipeline (set by the last step).
        thinking:       chain-of-thought output captured from a deep-think step.
        tool_calls:     tool calls extracted from a model response.
        tool_results:   results of executed tool calls (step can populate this).
        metadata:       open dict for arbitrary inter-step communication.
        errors:         list of non-fatal warnings / errors accumulated during the run.
    """

    # -- primary I/O --------------------------------------------------------
    query: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    result: str = ""

    # -- attachments --------------------------------------------------------
    attachments: list[Attachment] = field(default_factory=list)

    # -- optional enrichments -----------------------------------------------
    thinking: str | None = None
    tool_calls: list[dict] | None = None
    tool_results: list[dict] | None = None

    # -- open-ended ---------------------------------------------------------
    metadata: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    # Agent-loop nudge flags (one-shot retries for empty / fabricated answers).
    _empty_nudge_sent: bool = False
    _fabrication_nudge_sent: bool = False

    # -- convenience helpers ------------------------------------------------

    def add_user_message(self, content: str) -> None:
        """Append a user message to the conversation."""
        self.messages.append({"role": "user", "content": content})

    def add_system_message(self, content: str) -> None:
        """Append (or replace) a system message at the front."""
        if self.messages and self.messages[0]["role"] == "system":
            self.messages[0] = {"role": "system", "content": content}
        else:
            self.messages.insert(0, {"role": "system", "content": content})

    def add_assistant_message(self, content: str) -> None:
        """Append an assistant message to the conversation."""
        self.messages.append({"role": "assistant", "content": content})

    def add_error(self, error: str) -> None:
        """Record a non-fatal error for later inspection."""
        logger.warning("Pipeline error: %s", error)
        self.errors.append(error)

    @property
    def last_assistant_content(self) -> str | None:
        """Return the content of the most recent assistant message, or *None*."""
        for msg in reversed(self.messages):
            if msg["role"] == "assistant":
                return msg.get("content")
        return None

    def clone(self) -> PipelineContext:
        """Return a shallow copy (messages list is copied, dicts inside are shared)."""
        return PipelineContext(
            query=self.query,
            messages=list(self.messages),
            result=self.result,
            attachments=list(self.attachments),
            thinking=self.thinking,
            tool_calls=list(self.tool_calls) if self.tool_calls else None,
            tool_results=list(self.tool_results) if self.tool_results else None,
            metadata=dict(self.metadata),
            errors=list(self.errors),
        )
