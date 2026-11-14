"""Per-caller model instructions.

The admin, and a request with no caller, use the ``user_context`` file.
Every other user uses the ``instructions`` column on their row. Nothing
in here writes that file.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import yaml

from app.config import settings
from web.server.database.manager import ChatDatabase
from web.server.identity import ADMIN_USER_ID, current_user_id

logger = logging.getLogger(__name__)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_NAME_RE = re.compile(r"^\s*[-*+]?\s*name\s*:\s*(.+)", re.MULTILINE | re.IGNORECASE)

_chat_db: ChatDatabase | None = None


def admin_context_path() -> Path | None:
    """Resolved ``user_context`` file, or None when the setting is empty."""
    file_setting = (settings.user_context_file or "").strip()
    if not file_setting:
        return None
    path = Path(file_setting)
    if not path.is_absolute():
        path = _SERVICE_ROOT / path
    return path


def read_admin_context() -> str:
    """Admin instructions from the file. Empty when it is missing."""
    path = admin_context_path()
    if path is None or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not read user context file %s: %s", path, exc)
        return ""


def first_name(text: str) -> str:
    """First word of a ``Name:`` line, or empty."""
    match = _NAME_RE.search(text or "")
    if not match:
        return ""
    full_name = match.group(1).strip()
    return full_name.split()[0] if full_name else ""


def instructions_for_caller() -> str:
    """Text appended to this caller's system prompt.

    Admin and no caller read the file, even when the admin row has its own
    column filled in. Another user with an empty column gets nothing, not
    the file.
    """
    actor = current_user_id()
    if not actor or actor == ADMIN_USER_ID:
        return read_admin_context()
    try:
        user = _user_row(actor)
    except Exception:
        logger.warning("user instructions lookup failed for %s", actor, exc_info=True)
        return ""
    if user is None:
        return ""
    return (user.instructions or "").strip()


def _user_row(user_id: str):
    # web.server.api imports this module. Importing get_db up here cycles.
    from web.server.api import get_db

    try:
        db = get_db()
    except RuntimeError:
        db = _worker_chat_db()
    return db.get_user(user_id)


def _chat_db_path() -> Path:
    db_rel = "data/web_chat.db"
    config_path = _SERVICE_ROOT / "config.yaml"
    if config_path.is_file():
        try:
            with open(config_path, encoding="utf-8") as handle:
                cfg = yaml.safe_load(handle) or {}
            db_rel = (cfg.get("web") or {}).get("database_path", db_rel) or db_rel
        except OSError:
            logger.debug("chat database path stayed at the default", exc_info=True)
    path = Path(str(db_rel))
    if not path.is_absolute():
        path = _SERVICE_ROOT / path
    return path


def _worker_chat_db() -> ChatDatabase:
    """SQLite handle for a worker process that never ran the web lifespan.

    Tests keep the web singleton set. Opening the configured database from
    a test would read the live chat file.
    """
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("chat database is not initialised")
    global _chat_db
    if _chat_db is None:
        _chat_db = ChatDatabase(_chat_db_path())
    return _chat_db
