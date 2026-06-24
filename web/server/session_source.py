"""Resolve ``chat_sessions.source`` for multi-client session namespacing.

External apps (Knowledge Base SPA, Slack bot, etc.) tag sessions so the
Agent Chat sidebar can filter them out. Source is write-once at creation.
"""

from __future__ import annotations

from typing import Any

from .queue.store import job_store


def normalize_source(value: str | None, *, default: str = "web") -> str:
    """Return a lowercase client tag, falling back to *default* when empty."""
    if value is None:
        return default
    cleaned = str(value).strip().lower()
    return cleaned or default


def resolve_session_source(
    session_id: str,
    *,
    connect_source: str | None = None,
    overrides: dict[str, Any] | None = None,
    default: str = "web",
) -> str:
    """Pick the source tag for a new session row.

    Priority: query ``overrides.source`` > WebSocket ``?source=`` connect param
    > active worker job overrides > *default* (``web`` for the Agent Chat UI).
    """
    if overrides:
        src = overrides.get("source")
        if src is not None and str(src).strip():
            return normalize_source(str(src), default=default)

    if connect_source and str(connect_source).strip():
        return normalize_source(connect_source, default=default)

    job = job_store.get_active_job(session_id)
    if job and job.overrides:
        src = job.overrides.get("source")
        if src is not None and str(src).strip():
            return normalize_source(str(src), default=default)

    return default
