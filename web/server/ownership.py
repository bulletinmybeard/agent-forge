"""Session ownership for routes that do not go through ChatDatabase.get_session."""

from __future__ import annotations


def _actor_and_owner(session_id: str) -> tuple[str | None, str | None]:
    from web.server.identity import current_user_id

    actor = current_user_id()
    if not actor:
        return None, None
    from web.server.api import get_db

    return actor, get_db().get_session_owner(session_id)


def session_owned(session_id: str) -> bool:
    """True when auth is off, or the current user owns ``session_id``.

    A missing session is not owned. Callers answer that with 404.
    """
    actor, owner = _actor_and_owner(session_id)
    if not actor:
        return True
    return owner == actor


def session_writable(session_id: str) -> bool:
    """False only when the session exists and belongs to someone else.

    A session id that has no row yet can still receive an upload. Reading it
    stays on :func:`session_owned`.
    """
    actor, owner = _actor_and_owner(session_id)
    if not actor:
        return True
    return owner is None or owner == actor
