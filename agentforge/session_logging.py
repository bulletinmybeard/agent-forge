"""Stamp the request-scoped chat session UUID onto log records.

Reads ``get_request_session_id()``. When it is a full UUID, appends
`` session_id=<uuid>`` to the rendered message so Grafana ``|=`` and an Alloy
regex both work. Does not install a custom Formatter — uvicorn / saq / launchd
keep their own format.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agentforge.config import get_request_session_id, set_request_session_id

SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

_FILTER_FLAG = "_agentforge_session_id_filter"


class SessionIdFilter(logging.Filter):
    """Append `` session_id=<uuid>`` when the ContextVar holds a real UUID."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            sid = get_request_session_id()
        except Exception:
            return True
        if not sid or not SESSION_ID_RE.match(sid):
            return True
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        suffix = f"session_id={sid}"
        if suffix in rendered:
            return True
        record.msg = f"{rendered} {suffix}"
        record.args = ()
        return True


def configure_session_logging() -> None:
    """Attach ``SessionIdFilter`` to root / uvicorn / saq loggers and handlers.

    Idempotent. Safe to call from FastAPI lifespan and SAQ settings import.
    """
    filt = SessionIdFilter()
    loggers = [
        logging.getLogger(),
        logging.getLogger("uvicorn"),
        logging.getLogger("uvicorn.error"),
        logging.getLogger("saq"),
    ]
    for logger in loggers:
        _ensure_filter(logger, filt)
        for handler in logger.handlers:
            _ensure_filter(handler, filt)
    last_resort = getattr(logging, "lastResort", None)
    if last_resort is not None:
        _ensure_filter(last_resort, filt)


def _ensure_filter(target: logging.Logger | logging.Handler, filt: SessionIdFilter) -> None:
    existing = getattr(target, _FILTER_FLAG, False)
    if existing:
        return
    target.addFilter(filt)
    setattr(target, _FILTER_FLAG, True)


def bind_session_from_job(job: Any) -> None:
    """Set the ContextVar from a SAQ job's ``kwargs['session_id']``."""
    kwargs = getattr(job, "kwargs", None) or {}
    sid = kwargs.get("session_id") if isinstance(kwargs, dict) else None
    if isinstance(sid, str) and SESSION_ID_RE.match(sid):
        set_request_session_id(sid)
    else:
        set_request_session_id(None)


def clear_session_binding() -> None:
    """Clear the request-scoped session id. Call from SAQ after_process."""
    set_request_session_id(None)
