"""Delete chat user data. Does not touch third-party connections.

POST /api/user-data/delete with ``{"confirm": "delete"}``.

A POST body is required on purpose: some clients drop a DELETE body, and a
bare DELETE must not be enough to wipe the store.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .api import _SAFE_SESSION_RE, get_db, get_upload_base
from .conversation_memory import get_conversation_memory

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user-data")

_CONFIRM = "delete"
# Session uploads are ``{upload_base}/{session_id}/``. These two siblings are
# operational (monitor captures and the screenshot helper), not chat attachments.
_KEEP_UPLOAD_DIRS = frozenset({"monitor", "screenshots"})
_KEPT = [
    "connections",
    "command_notes",
    "command_policies",
    "scheduled_jobs",
    "monitor_jobs",
    "trips",
    "knowledge_collections",
]


class UserDataDeleteRequest(BaseModel):
    confirm: str


def clear_session_attachments(upload_base: Path | None) -> int:
    """Remove session upload directories. Returns how many directories went.

    Skips the operational directories in ``_KEEP_UPLOAD_DIRS``, symlinks, and
    names that could not have been created by the upload route.
    """
    if upload_base is None or not upload_base.is_dir():
        return 0
    removed = 0
    for child in upload_base.iterdir():
        if child.is_symlink() or not child.is_dir():
            continue
        if child.name in _KEEP_UPLOAD_DIRS or not _SAFE_SESSION_RE.match(child.name):
            continue
        shutil.rmtree(child)
        removed += 1
    return removed


def _memory_status() -> str:
    """Clear points in ``conversation_memory``. The collection stays.

    ``unavailable`` means the memory client was never started. ``failed`` means
    a live client could not delete. Other Qdrant collections are not touched.
    """
    mem = get_conversation_memory()
    if mem is None:
        return "unavailable"
    try:
        return "cleared" if mem.delete_all() else "failed"
    except Exception as exc:
        logger.warning("user-data delete: conversation memory clear failed: %s", exc)
        return "failed"


@router.post("/delete")
def delete_user_data(body: UserDataDeleteRequest):
    """Delete chat sessions, attachments, and conversation memory."""
    if body.confirm != _CONFIRM:
        raise HTTPException(status_code=400, detail='confirm must be "delete"')

    deleted = get_db().delete_user_chat_data()
    deleted["attachments"] = clear_session_attachments(get_upload_base())
    deleted["conversation_memory"] = _memory_status()
    payload = {"deleted": deleted, "kept": list(_KEPT)}
    if deleted["conversation_memory"] == "failed":
        payload["error"] = "Conversation memory was not cleared"
        return JSONResponse(status_code=502, content=payload)
    return payload
