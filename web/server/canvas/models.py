"""Canvas items — ORM model on the shared chat SQLite database.

Tables live in the same file as chat sessions (see ``CanvasDatabase``).
Schema is managed by the **chat** Alembic tree under
``web/server/database/migrations/``.
"""

from __future__ import annotations

from sqlalchemy import Column, Index, Integer, String, Text, UniqueConstraint

from web.server.database.models import Base


class CanvasItem(Base):
    __tablename__ = "canvas_items"
    __table_args__ = (
        UniqueConstraint("session_id", "type", "content", name="uq_canvas_session_type_content"),
        Index("canvas_items_session", "session_id", "footnote_num"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(String, nullable=False)
    type = Column(String, nullable=False)
    content = Column(Text, nullable=False)
    label = Column(String, nullable=True)
    footnote_num = Column(Integer, nullable=False)
    created_at = Column(String, nullable=False)  # ISO text, matches historical schema
