"""A hidden session is one 404. The process log says missing or denied."""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security import install_api_key_auth, set_auth_database
from web.server import api as api_module
from web.server.api import internal as internal_router
from web.server.api import router as api_router
from web.server.api import set_database
from web.server.auth_api import router as auth_router
from web.server.auth_api import users_router
from web.server.database import ChatDatabase
from web.server.identity import ADMIN_USER_ID, hash_password, reset_current_user, set_current_user
from web.server.ownership import session_unavailable_reason


def test_reason_splits_missing_and_denied():
    assert session_unavailable_reason(None, "user-a") == "missing"
    assert session_unavailable_reason(None, None) == "missing"
    assert session_unavailable_reason("user-b", "user-a") == "denied"
    assert session_unavailable_reason("user-a", "user-a") == "unavailable"
    assert session_unavailable_reason("user-a", None) == "unavailable"


def _app(db: ChatDatabase) -> FastAPI:
    app = FastAPI()
    install_api_key_auth(app)
    app.include_router(api_router)
    app.include_router(internal_router)
    app.include_router(auth_router)
    app.include_router(users_router)
    set_database(db)
    set_auth_database(db)
    return app


def test_hidden_session_is_one_404(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    db.create_session("admin-chat", title="Admin")
    token = set_current_user(personal.id)
    try:
        db.create_session("personal-chat", title="Personal")
    finally:
        reset_current_user(token)

    lines: list[str] = []

    class _Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    log = logging.getLogger("web.server.sessions")
    grab = _Grab()
    log.addHandler(grab)
    previous = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        logged = client.post(
            "/api/auth/login",
            json={"username": "personal", "password": "personal-pass-1"},
        )
        assert logged.status_code == 200
        missing = client.get("/api/sessions/missing-chat")
        denied = client.get("/api/sessions/admin-chat")
        own = client.get("/api/sessions/personal-chat")
    finally:
        log.removeHandler(grab)
        set_auth_database(None)
        api_module.set_database(previous)

    assert missing.status_code == 404
    assert denied.status_code == 404
    assert missing.json() == denied.json() == {"detail": "Session not found"}
    assert "missing" not in missing.text
    assert "denied" not in denied.text
    assert "owner" not in denied.text
    assert own.status_code == 200
    assert own.json()["id"] == "personal-chat"
    assert "session missing-chat missing" in lines
    assert f"session admin-chat denied for user {personal.id} (owner {ADMIN_USER_ID})" in lines
