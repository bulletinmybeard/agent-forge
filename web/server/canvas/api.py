"""REST API — Canvas endpoints for session-scoped scratch pad items.

Mounted at /api/canvas by the main app.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .database import CanvasDatabase

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/canvas", tags=["canvas"])

_db: CanvasDatabase | None = None


def init_canvas_api(db: CanvasDatabase) -> None:
    """Called from app.py lifespan to inject the database."""
    global _db
    _db = db
    logger.info("Canvas API initialised")


# ── Request models ────────────────────────────────────────────────────────


class AddItemRequest(BaseModel):
    type: str
    content: str
    label: str | None = None


class UpdateItemRequest(BaseModel):
    content: str
    label: str | None = None


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/{session_id}")
async def get_canvas(session_id: str):
    """Return all canvas items for a session, ordered by footnote_num."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Canvas database not initialised")
    items = _db.get_items(session_id)
    return {"items": items}


@router.post("/{session_id}")
async def add_canvas_item(session_id: str, body: AddItemRequest):
    """Add an item to the session canvas. Idempotent — returns existing on duplicate."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Canvas database not initialised")
    return _db.add_item(session_id, body.type, body.content, body.label)


@router.delete("/{session_id}/{item_id}")
async def delete_canvas_item(session_id: str, item_id: int):
    """Delete a canvas item by ID."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Canvas database not initialised")
    deleted = _db.delete_item(session_id, item_id)
    return {"deleted": deleted}


@router.patch("/{session_id}/{item_id}")
async def update_canvas_item(session_id: str, item_id: int, body: UpdateItemRequest):
    """Update content and label for a canvas item (note items)."""
    if _db is None:
        raise HTTPException(status_code=503, detail="Canvas database not initialised")
    item = _db.update_item(session_id, item_id, body.content, body.label)
    if item is None:
        raise HTTPException(status_code=404, detail=f"Canvas item {item_id} not found")
    return item
