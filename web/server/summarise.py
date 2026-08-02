"""Shared conversation flattening for LLM summarisation.

Session compaction (``ws_endpoint._compact_session``) and the recap endpoint
(``api.create_recap``) both need the conversation rendered as plain text. Only
their prompts differ, so the rendering lives here to keep the two from drifting
apart.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .database.models import ChatMessage

# Per-message ceiling so one huge result can't crowd out the rest of the prompt.
_MAX_RESULT_CHARS = 2000

DEFAULT_MAX_ENTRIES = 60

# Message types that carry conversational signal. Everything else (config,
# routing, cancelled, summary, recap) is chrome and gets skipped.
SUMMARISABLE_TYPES = ("query", "result", "tool_calls")

RECAP_MESSAGE_TYPE = "recap"


def _msg_str(value: Any) -> str | None:
    """Coerce SQLAlchemy instance attrs (typed as Column at class level) to str."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _msg_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    return int(value)


def flatten_messages_for_summary(
    messages: list[ChatMessage],
    max_entries: int = DEFAULT_MAX_ENTRIES,
) -> str:
    """Render chat messages as ``User:`` / ``Assistant:`` / ``Tools called:`` lines.

    Keeps only the last *max_entries* rendered lines. Returns an empty string
    when nothing summarisable is present, which callers treat as "nothing to do".
    """
    lines: list[str] = []
    for msg in messages:
        msg_type = _msg_str(msg.type)
        content = _msg_str(msg.content)
        if msg_type == "query" and content:
            lines.append(f"User: {content}")
        elif msg_type == "result" and content:
            text = content[:_MAX_RESULT_CHARS] + "..." if len(content) > _MAX_RESULT_CHARS else content
            lines.append(f"Assistant: {text}")
        elif msg_type == "tool_calls":
            raw_calls = msg.tool_calls_json
            if not raw_calls:
                continue
            try:
                calls = json.loads(raw_calls) if isinstance(raw_calls, str) else raw_calls
                names = [c.get("name", "?") for c in (calls or [])]
                lines.append(f"Tools called: {', '.join(names)}")
            except Exception:
                pass

    if not lines:
        return ""
    return "\n".join(lines[-max_entries:])


_FENCE_LINE_RE = re.compile(r"^[ \t]*```[a-zA-Z0-9_+-]*[ \t]*$", re.MULTILINE)
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_HEADING_RE = re.compile(r"^[ \t]*#{1,6}[ \t]*", re.MULTILINE)
_LIST_RE = re.compile(r"^[ \t]*(?:[-*+]|\d+\.)[ \t]+", re.MULTILINE)


def strip_markdown(text: str) -> str:
    """Flatten a model answer to a single plain-text paragraph.

    Recaps render as plain text, so any markdown the model emits — backticks
    around identifiers especially — shows up literally in the UI.

    Underscores are deliberately left alone: they appear in real identifiers
    (``ends_at``, ``min_new_messages``) far more often than as emphasis, and
    stripping them would corrupt the names a recap exists to preserve.
    """
    if not text:
        return ""
    out = _FENCE_LINE_RE.sub("", text)
    out = _LINK_RE.sub(r"\1", out)
    out = _BOLD_RE.sub(r"\1", out)
    out = _ITALIC_RE.sub(r"\1", out)
    out = _HEADING_RE.sub("", out)
    out = _LIST_RE.sub("", out)
    out = out.replace("`", "")
    # A recap is one paragraph — fold any line breaks into spaces.
    return " ".join(out.split())


def select_recap_window(
    messages: list[ChatMessage],
    recap_type: str = RECAP_MESSAGE_TYPE,
) -> tuple[list[ChatMessage], int, str | None]:
    """Split *messages* at the most recent recap.

    Returns ``(fresh, watermark, previous_text)`` — the summarisable messages
    after the last recap, that recap's ``sequence`` (0 when none exists), and its
    text. Because a recap is itself a stored message, its sequence *is* the
    watermark; no separate bookkeeping is needed.

    Volatile rows are excluded (they are chrome the model never sees, recaps
    included) and so are incognito rows, which must not end up baked into a
    persisted summary.
    """
    watermark = 0
    previous: str | None = None
    for msg in messages:
        if _msg_str(msg.type) == recap_type:
            watermark = _msg_int(msg.sequence)
            previous = _msg_str(msg.content)

    fresh = [
        m
        for m in messages
        if _msg_int(m.sequence) > watermark
        and not bool(m.is_volatile)
        and not bool(m.is_incognito)
        and _msg_str(m.type) in SUMMARISABLE_TYPES
    ]
    return fresh, watermark, previous
