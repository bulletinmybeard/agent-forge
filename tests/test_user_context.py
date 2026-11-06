"""Model instructions: the admin file stays put, other users have their own text."""

from __future__ import annotations

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.security import install_api_key_auth, set_auth_database
from web.server import api as api_module
from web.server.api import get_welcome
from web.server.api import router as api_router
from web.server.auth_api import router as auth_router
from web.server.database import ChatDatabase
from web.server.database.models import User
from web.server.identity import (
    ADMIN_USER_ID,
    hash_password,
    reset_current_user,
    set_current_user,
)
from web.server.user_context import first_name, instructions_for_caller
from web.server.ws_endpoint import _inject_user_context

_FILE = "- Name: Ada Lovelace\nlikes files\n"
_OTHER = "- Name: Nia Example\nkeeps lists\n"


def _file(tmp_path, monkeypatch):
    path = tmp_path / "user_context.md"
    path.write_text(_FILE, encoding="utf-8")
    monkeypatch.setattr(settings, "user_context_file", str(path))
    return path


def _db(tmp_path) -> tuple[ChatDatabase, User]:
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    other = db.create_user("personal", hash_password("personal-pass-1"))
    with db.SessionLocal() as session:
        row = session.get(User, ADMIN_USER_ID)
        row.instructions = "- Name: Notfile\nstolen"
        session.commit()
    return db, other


def test_instructions_follow_the_caller(tmp_path, monkeypatch):
    path = _file(tmp_path, monkeypatch)
    db, other = _db(tmp_path)
    previous = api_module._db
    api_module.set_database(db)
    try:
        token = set_current_user(None)
        try:
            assert instructions_for_caller() == _FILE.strip()
            assert first_name(instructions_for_caller()) == "Ada"
            assert "likes files" in _inject_user_context("base", None)
            welcome = asyncio.run(get_welcome())
        finally:
            reset_current_user(token)
        assert welcome["name"] == "Ada"
        assert path.read_text(encoding="utf-8") == _FILE

        token = set_current_user(ADMIN_USER_ID)
        try:
            assert "Notfile" not in instructions_for_caller()
            assert "likes files" in instructions_for_caller()
        finally:
            reset_current_user(token)

        token = set_current_user(other.id)
        try:
            assert instructions_for_caller() == ""
            assert _inject_user_context("base", None) == "base"
            welcome = asyncio.run(get_welcome())
            assert welcome["name"] == ""
            assert "Ada" not in welcome["headline"]

            db.set_user_instructions(other.id, _OTHER)
            assert instructions_for_caller() == _OTHER.strip()
            assert first_name(instructions_for_caller()) == "Nia"
            prompt = _inject_user_context("base", None)
            welcome = asyncio.run(get_welcome())
        finally:
            reset_current_user(token)

        assert "keeps lists" in prompt
        assert "likes files" not in prompt
        assert welcome["name"] == "Nia"
        assert "Ada" not in welcome["headline"]
        assert path.read_text(encoding="utf-8") == _FILE
    finally:
        api_module.set_database(previous)


def test_instruction_save_does_not_touch_the_admin_file(tmp_path, monkeypatch):
    path = _file(tmp_path, monkeypatch)
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db, _other = _db(tmp_path)
    previous = api_module._db
    app = FastAPI()
    install_api_key_auth(app)
    app.include_router(api_router)
    app.include_router(auth_router)
    api_module.set_database(db)
    set_auth_database(db)
    try:
        client = TestClient(app)
        admin = {"Authorization": "Bearer deploy-key"}

        seen = client.get("/api/auth/instructions", headers=admin)
        assert seen.status_code == 200
        assert seen.json() == {"text": _FILE.strip(), "editable": False}
        assert client.get("/api/welcome", headers=admin).json()["name"] == "Ada"

        refused = client.put("/api/auth/instructions", headers=admin, json={"text": "overwrite the file"})
        assert refused.status_code == 403
        assert path.read_text(encoding="utf-8") == _FILE

        logged_in = client.post(
            "/api/auth/login",
            json={"username": "personal", "password": "personal-pass-1"},
        )
        assert logged_in.status_code == 200
        assert client.get("/api/auth/instructions").json() == {"text": "", "editable": True}
        assert client.get("/api/welcome").json()["name"] == ""

        saved = client.put("/api/auth/instructions", json={"text": _OTHER})
        assert saved.status_code == 200
        assert saved.json()["editable"] is True
        assert saved.json()["text"] == _OTHER.strip()
        welcome = client.get("/api/welcome").json()
        assert welcome["name"] == "Nia"
        assert "Ada" not in welcome["headline"]
        assert path.read_text(encoding="utf-8") == _FILE
        assert "overwrite" not in path.read_text(encoding="utf-8")
    finally:
        set_auth_database(None)
        api_module.set_database(previous)
