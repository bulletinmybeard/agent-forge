"""CanvasDatabase — persistence for session-scoped canvas items.

Same SQLAlchemy patterns as ChatDatabase and WorklogDatabase:
  - NullPool for safe cross-process/cross-container access
  - journal_mode=DELETE (no WAL shared-memory files)
  - Raw SQL via text() — no ORM models
  - Idempotent create_tables()
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from web.server.database.migrate import upgrade as alembic_upgrade

logger = logging.getLogger(__name__)


def _set_sqlite_pragmas(dbapi_conn, connection_record) -> None:
    cursor = dbapi_conn.execute("PRAGMA journal_mode=DELETE")
    cursor.close()
    cursor = dbapi_conn.execute("PRAGMA foreign_keys=ON")
    cursor.close()


class CanvasDatabase:
    """SQLite-backed storage for canvas items."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        logger.info("CanvasDatabase initialised at %s", self.db_path)

    def create_tables(self) -> None:
        """Apply chat-DB Alembic migrations (includes ``canvas_items``)."""
        alembic_upgrade(self.db_path, database="chat")
        logger.info("Canvas schema ready via chat Alembic at %s", self.db_path)

    def _row_to_dict(self, row) -> dict:
        return {
            "id": row.id,
            "session_id": row.session_id,
            "type": row.type,
            "content": row.content,
            "label": row.label,
            "footnote_num": row.footnote_num,
            "created_at": row.created_at,
        }

    def add_item(
        self,
        session_id: str,
        type: str,
        content: str,
        label: str | None = None,
    ) -> dict:
        """Insert a canvas item for a session.

        Assigns the next footnote_num sequentially within the session.
        On UNIQUE conflict (same session_id + type + content), returns the existing row.
        """
        with self.SessionLocal() as session:
            # Determine next footnote_num for this session
            result = session.execute(
                text("SELECT MAX(footnote_num) FROM canvas_items WHERE session_id = :sid"),
                {"sid": session_id},
            ).scalar()
            next_num = (result or 0) + 1
            created_at = datetime.now().isoformat()

            try:
                session.execute(
                    text("""
                        INSERT INTO canvas_items (session_id, type, content, label, footnote_num, created_at)
                        VALUES (:sid, :type, :content, :label, :num, :created_at)
                    """),
                    {
                        "sid": session_id,
                        "type": type,
                        "content": content,
                        "label": label,
                        "num": next_num,
                        "created_at": created_at,
                    },
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                # Return the existing row
                row = session.execute(
                    text("""
                        SELECT id, session_id, type, content, label, footnote_num, created_at
                        FROM canvas_items
                        WHERE session_id = :sid AND type = :type AND content = :content
                    """),
                    {"sid": session_id, "type": type, "content": content},
                ).fetchone()
                return self._row_to_dict(row)

            row = session.execute(
                text("""
                    SELECT id, session_id, type, content, label, footnote_num, created_at
                    FROM canvas_items
                    WHERE session_id = :sid AND type = :type AND content = :content
                """),
                {"sid": session_id, "type": type, "content": content},
            ).fetchone()
            return self._row_to_dict(row)

    def get_items(self, session_id: str) -> list[dict]:
        """Return all canvas items for a session, ordered by footnote_num."""
        with self.SessionLocal() as session:
            rows = session.execute(
                text("""
                    SELECT id, session_id, type, content, label, footnote_num, created_at
                    FROM canvas_items
                    WHERE session_id = :sid
                    ORDER BY footnote_num ASC
                """),
                {"sid": session_id},
            ).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def delete_item(self, session_id: str, item_id: int) -> bool:
        """Delete a canvas item by ID within a session. Returns True if deleted."""
        with self.SessionLocal() as session:
            result = session.execute(
                text("DELETE FROM canvas_items WHERE id = :id AND session_id = :sid"),
                {"id": item_id, "sid": session_id},
            )
            session.commit()
            return result.rowcount > 0

    def update_item(self, session_id: str, item_id: int, content: str, label: str | None) -> dict | None:
        """Update content and label for a canvas item (intended for note items).

        Returns the updated item dict, or None if not found.
        """
        with self.SessionLocal() as session:
            result = session.execute(
                text("""
                    UPDATE canvas_items
                    SET content = :content, label = :label
                    WHERE id = :id AND session_id = :sid
                """),
                {"content": content, "label": label, "id": item_id, "sid": session_id},
            )
            session.commit()
            if result.rowcount == 0:
                return None
            row = session.execute(
                text("""
                    SELECT id, session_id, type, content, label, footnote_num, created_at
                    FROM canvas_items
                    WHERE id = :id AND session_id = :sid
                """),
                {"id": item_id, "sid": session_id},
            ).fetchone()
            return self._row_to_dict(row) if row else None
