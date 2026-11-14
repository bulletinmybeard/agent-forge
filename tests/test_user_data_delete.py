"""POST /api/user-data/delete clears chat data and leaves connections alone.

Uses a throwaway SQLite file and a throwaway upload directory. It does not
start the app lifespan, so it never touches the live chat database.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from web.server import api as api_module
from web.server.api_user_data import router as user_data_router
from web.server.database import ChatDatabase
from web.server.database.models import (
    ChatMessage,
    CommandNote,
    CommandPolicyOverride,
    Connection,
    ScheduledJob,
)


class _Memory:
    def __init__(self, ok: bool = True, boom: bool = False):
        self.calls = 0
        self.ok = ok
        self.boom = boom

    def delete_all(self) -> bool:
        self.calls += 1
        if self.boom:
            raise RuntimeError("qdrant down")
        return self.ok


@pytest.fixture
def world(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.create_session("root", title="Root")
    db.create_session("child", parent_session_id="root", title="Child")
    db.create_session("ext", source="cli", title="External")
    db.add_message("root", role="user", msg_type="query", content="hi")
    db.upsert_fact("preference", "editor", "vim")
    db.add_session_instruction("be terse")
    db.add_session_instruction("this chat", session_id="root")

    with db.SessionLocal() as session:
        session.add(
            Connection(
                id="conn-1",
                connector_type="gmail",
                label="Work",
                encrypted_tokens="not-a-real-token",
            )
        )
        session.add(
            CommandNote(
                session_id="root",
                title="keep me",
                commands_json="[]",
            )
        )
        session.add(
            CommandPolicyOverride(
                tool="shell",
                mode="confirm",
                allowed_commands=[],
                allowed_patterns=[],
                blocked_patterns=[],
            )
        )
        session.add(ScheduledJob(id="job-1", label="keep", command="echo hi", cron="0 0 * * *"))
        session.execute(
            text(
                "INSERT INTO canvas_items (session_id, type, content, label, footnote_num, created_at) "
                "VALUES ('root', 'note', 'hello', NULL, 1, '2026-01-01')"
            )
        )
        session.commit()

    uploads = tmp_path / "uploads"
    (uploads / "root").mkdir(parents=True)
    (uploads / "root" / "note.txt").write_text("secret-ish", encoding="utf-8")
    (uploads / "orphan-session").mkdir()
    (uploads / "monitor" / "screenshots").mkdir(parents=True)
    (uploads / "monitor" / "screenshots" / "a.png").write_bytes(b"png")
    (uploads / "screenshots").mkdir()
    (uploads / "screenshots" / "b.png").write_bytes(b"png")
    (uploads / "not a session").mkdir()

    prev = (api_module._db, api_module._upload_base, api_module._max_file_size, api_module._max_files)
    api_module.set_database(db)
    api_module.set_upload_config(uploads)
    mem = _Memory()
    monkeypatch.setattr("web.server.api_user_data.get_conversation_memory", lambda: mem)

    app = FastAPI()
    app.include_router(user_data_router)
    try:
        yield TestClient(app), db, uploads, mem
    finally:
        api_module._db, api_module._upload_base, api_module._max_file_size, api_module._max_files = prev


def test_rejects_a_request_without_the_confirm_token(world):
    client, db, uploads, mem = world

    response = client.post("/api/user-data/delete", json={"confirm": "yes"})

    assert response.status_code == 400
    assert db.get_session("root") is not None
    assert db.get_all_facts()
    assert (uploads / "root" / "note.txt").is_file()
    assert mem.calls == 0


def test_missing_body_does_not_delete(world):
    client, db, _uploads, mem = world

    response = client.post("/api/user-data/delete")

    assert response.status_code == 422
    assert db.get_session("root") is not None
    assert mem.calls == 0


def test_delete_clears_chat_data_and_keeps_connections(world):
    client, db, uploads, mem = world

    response = client.post("/api/user-data/delete", json={"confirm": "delete"})

    assert response.status_code == 200
    body = response.json()
    assert body["deleted"] == {
        "sessions": 3,
        "facts": 1,
        "instructions": 2,
        "canvas_items": 1,
        "attachments": 2,
        "conversation_memory": "cleared",
    }
    assert "connections" in body["kept"]
    assert mem.calls == 1

    assert db.get_session("root") is None
    assert db.get_session("child") is None
    assert db.get_session("ext") is None
    assert db.get_all_facts() == []
    assert db.get_global_instructions() == []
    with db.SessionLocal() as session:
        assert session.query(ChatMessage).count() == 0
        assert session.execute(text("SELECT COUNT(*) FROM canvas_items")).scalar() == 0
        note = session.query(CommandNote).one()
        assert note.title == "keep me"
        assert note.session_id is None
        conn = session.query(Connection).one()
        assert conn.id == "conn-1"
        assert conn.connector_type == "gmail"
        assert session.query(CommandPolicyOverride).count() == 1
        assert session.query(ScheduledJob).count() == 1

    assert not (uploads / "root").exists()
    assert not (uploads / "orphan-session").exists()
    assert (uploads / "monitor" / "screenshots" / "a.png").is_file()
    assert (uploads / "screenshots" / "b.png").is_file()
    assert (uploads / "not a session").is_dir()


def test_second_delete_is_a_no_op(world):
    client, _db, _uploads, mem = world
    client.post("/api/user-data/delete", json={"confirm": "delete"})

    response = client.post("/api/user-data/delete", json={"confirm": "delete"})

    assert response.status_code == 200
    deleted = response.json()["deleted"]
    assert deleted["sessions"] == 0
    assert deleted["attachments"] == 0
    assert deleted["conversation_memory"] == "cleared"
    assert mem.calls == 2


def test_unavailable_memory_still_clears_sqlite(world, monkeypatch):
    client, db, _uploads, _mem = world
    monkeypatch.setattr("web.server.api_user_data.get_conversation_memory", lambda: None)

    response = client.post("/api/user-data/delete", json={"confirm": "delete"})

    assert response.status_code == 200
    assert response.json()["deleted"]["conversation_memory"] == "unavailable"
    assert db.get_session("root") is None


def test_memory_failure_reports_502_after_sqlite_is_gone(world, monkeypatch):
    client, db, _uploads, _mem = world
    monkeypatch.setattr("web.server.api_user_data.get_conversation_memory", lambda: _Memory(ok=False))

    response = client.post("/api/user-data/delete", json={"confirm": "delete"})

    assert response.status_code == 502
    body = response.json()
    assert body["deleted"]["conversation_memory"] == "failed"
    assert body["deleted"]["sessions"] == 3
    assert body["error"] == "Conversation memory was not cleared"
    assert db.get_session("root") is None
    with db.SessionLocal() as session:
        assert session.query(Connection).count() == 1
