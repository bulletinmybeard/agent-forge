"""Tests for Alembic multi-database migrations + schema_migrations log."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from web.server.database.migrate import (
    current,
    history,
    list_applied,
    upgrade,
    upgrade_all,
)
from web.server.database.models import Base

CHAT_HEAD = "002_canvas_items"
PROMPT_LAB_HEAD = "001_pl_baseline"


def test_upgrade_empty_chat_db_creates_schema(tmp_path: Path):
    db = tmp_path / "empty.db"
    upgrade(db, database="chat")
    assert current(db, database="chat") == CHAT_HEAD

    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()

    assert "chat_sessions" in tables
    assert "command_policy_overrides" in tables
    assert "canvas_items" in tables
    assert "alembic_version" in tables
    assert "schema_migrations" in tables


def test_schema_migrations_records_filenames(tmp_path: Path):
    db = tmp_path / "logged.db"
    upgrade(db, database="chat")
    rows = list_applied(db, database="chat")
    filenames = {r["filename"] for r in rows}
    revisions = {r["revision"] for r in rows}
    assert "001_baseline" in revisions
    assert "002_canvas_items" in revisions
    assert any("001_baseline" in f for f in filenames)
    assert any("002_canvas_items" in f or "canvas" in f for f in filenames)


def test_upgrade_is_idempotent(tmp_path: Path):
    db = tmp_path / "idem.db"
    upgrade(db, database="chat")
    upgrade(db, database="chat")
    assert current(db, database="chat") == CHAT_HEAD
    assert len(list_applied(db, database="chat")) == 2


def test_legacy_db_is_stamped_not_recreated(tmp_path: Path):
    db = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    try:
        Base.metadata.create_all(bind=engine)
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO chat_sessions (id, title, source, message_count, "
                    "prompt_tokens, completion_tokens, total_tokens, created_at, updated_at) "
                    "VALUES ('s1', 'Legacy', 'web', 0, 0, 0, 0, datetime('now'), datetime('now'))"
                )
            )
    finally:
        engine.dispose()

    assert current(db, database="chat") is None
    upgrade(db, database="chat")
    assert current(db, database="chat") == CHAT_HEAD

    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            row = conn.execute(text("SELECT title FROM chat_sessions WHERE id='s1'")).fetchone()
            assert row is not None
            assert row[0] == "Legacy"
    finally:
        engine.dispose()

    # Stamp path still populates schema_migrations
    assert len(list_applied(db, database="chat")) >= 1


def test_prompt_lab_baseline(tmp_path: Path):
    db = tmp_path / "pl.db"
    upgrade(db, database="prompt_lab")
    assert current(db, database="prompt_lab") == PROMPT_LAB_HEAD
    engine = create_engine(f"sqlite:///{db}", poolclass=NullPool)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "prompt_lab_runs" in tables
    assert "prompt_lab_results" in tables
    rows = list_applied(db, database="prompt_lab")
    assert any(r["revision"] == PROMPT_LAB_HEAD for r in rows)


def test_upgrade_all(tmp_path: Path, monkeypatch):
    chat = tmp_path / "chat.db"
    pl = tmp_path / "pl.db"
    results = upgrade_all(chat_db=chat, prompt_lab_db=pl)
    assert results["chat"] == CHAT_HEAD
    assert results["prompt_lab"] == PROMPT_LAB_HEAD


def test_history_includes_chat_revisions():
    revs = history(database="chat")
    assert "001_baseline" in revs
    assert "002_canvas_items" in revs


def test_chat_database_create_tables_uses_alembic(tmp_path: Path):
    from web.server.database.manager import ChatDatabase

    db_path = tmp_path / "via_manager.db"
    ChatDatabase(db_path).create_tables()
    assert current(db_path, database="chat") == CHAT_HEAD


def test_canvas_create_tables_uses_chat_alembic(tmp_path: Path):
    from web.server.canvas.database import CanvasDatabase

    db_path = tmp_path / "canvas.db"
    CanvasDatabase(db_path).create_tables()
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        assert "canvas_items" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
