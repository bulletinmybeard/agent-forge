"""Users: owner filter, login cookie, per-user keys, internal session guard."""

from __future__ import annotations

import importlib

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import NullPool

from app.security import install_api_key_auth, set_auth_database
from web.server import api as api_module
from web.server.api import internal as internal_router
from web.server.api import router as api_router
from web.server.api import set_database
from web.server.auth_api import router as auth_router
from web.server.auth_api import users_router
from web.server.database import ChatDatabase
from web.server.database.migrate import current, upgrade
from web.server.identity import (
    ADMIN_USER_ID,
    apply_admin_username,
    hash_password,
    reset_current_user,
    set_current_user,
)

_MIGRATION = importlib.import_module("web.server.database.migrations.versions.006_users")


def test_admin_id_matches_the_migration():
    assert _MIGRATION._ADMIN_USER_ID == ADMIN_USER_ID


def test_upgrade_from_005_backfills_admin(tmp_path):
    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('005_session_parent')"))
            conn.execute(
                text(
                    "CREATE TABLE chat_sessions ("
                    "id VARCHAR NOT NULL PRIMARY KEY, title VARCHAR, parent_session_id VARCHAR, "
                    "source VARCHAR, message_count INTEGER, prompt_tokens INTEGER, "
                    "completion_tokens INTEGER, total_tokens INTEGER, created_at DATETIME, updated_at DATETIME)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO chat_sessions (id, title, source, message_count, prompt_tokens, "
                    "completion_tokens, total_tokens, created_at, updated_at) "
                    "VALUES ('s1', 'Old', 'web', 0, 0, 0, 0, datetime('now'), datetime('now'))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE user_facts ("
                    "id INTEGER PRIMARY KEY, fact_type VARCHAR(30) NOT NULL, key VARCHAR(255) NOT NULL UNIQUE, "
                    "value TEXT NOT NULL, source_session VARCHAR(36), confidence FLOAT NOT NULL, "
                    "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO user_facts (fact_type, key, value, confidence, created_at, updated_at) "
                    "VALUES ('preference', 'editor', 'vim', 0.9, datetime('now'), datetime('now'))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE command_notes ("
                    "id INTEGER PRIMARY KEY, session_id VARCHAR(36), title VARCHAR(500) NOT NULL, "
                    "kind VARCHAR(20) NOT NULL, commands_json TEXT NOT NULL, content TEXT, "
                    "message_ts VARCHAR(50), created_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE session_instructions ("
                    "id INTEGER PRIMARY KEY, session_id VARCHAR(36), text TEXT NOT NULL, created_at DATETIME NOT NULL)"
                )
            )
    finally:
        engine.dispose()

    upgrade(db_path, database="chat")
    assert current(db_path, database="chat") == "008_impersonation"

    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            admin = conn.execute(text("SELECT username, is_admin FROM users WHERE id = :id"), {"id": ADMIN_USER_ID}).one()
            assert admin[0] == "admin"
            assert admin[1] in (1, True)
            owner = conn.execute(text("SELECT user_id FROM chat_sessions WHERE id = 's1'")).scalar()
            assert owner == ADMIN_USER_ID
            fact_owner = conn.execute(text("SELECT user_id FROM user_facts WHERE key = 'editor'")).scalar()
            assert fact_owner == ADMIN_USER_ID
            tables = set(inspect(engine).get_table_names())
            assert "api_keys" in tables
            assert "auth_sessions" in tables
    finally:
        engine.dispose()

    # Same key, two users. The old global unique would reject the second row.
    db = ChatDatabase(db_path)
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    token = set_current_user(personal.id)
    try:
        db.upsert_fact("preference", "editor", "vim")
    finally:
        reset_current_user(token)
    db.upsert_fact("preference", "editor", "emacs")
    token = set_current_user(personal.id)
    try:
        facts = db.get_all_facts()
    finally:
        reset_current_user(token)
    assert [fact.value for fact in facts] == ["vim"]


def test_sessions_do_not_cross_users(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.create_session("admin-chat", title="Admin")
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    token = set_current_user(personal.id)
    try:
        db.create_session("personal-chat", title="Personal")
        assert db.get_session("admin-chat") is None
        assert [row.id for row in db.list_sessions(sources=None)] == ["personal-chat"]
        assert db.get_messages("admin-chat") == []
    finally:
        reset_current_user(token)

    assert db.get_session("admin-chat") is not None
    assert db.get_session("personal-chat") is not None
    try:
        db.create_session("nope", parent_session_id="personal-chat")
    except ValueError as exc:
        assert "parent" in str(exc)
    else:
        raise AssertionError("admin context attached a child to someone else's session")


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


def test_login_cookie_and_keys_stay_on_that_user(tmp_path, monkeypatch):
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

    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        assert client.get("/api/auth/me").status_code == 401

        as_admin = client.get("/api/sessions", headers={"Authorization": "Bearer deploy-key"})
        assert as_admin.status_code == 200
        assert [row["id"] for row in as_admin.json()] == ["admin-chat"]

        bad = client.post("/api/auth/login", json={"username": "personal", "password": "nope"})
        assert bad.status_code == 401

        logged_in = client.post("/api/auth/login", json={"username": "personal", "password": "personal-pass-1"})
        assert logged_in.status_code == 200
        assert logged_in.json()["is_admin"] is False
        assert "af_session" in logged_in.cookies

        mine = client.get("/api/sessions")
        assert [row["id"] for row in mine.json()] == ["personal-chat"]
        assert client.get("/api/sessions/admin-chat").status_code == 404

        created = client.post("/api/auth/keys", json={"name": "mac"})
        assert created.status_code == 201
        secret = created.json()["secret"]
        assert secret.startswith(created.json()["prefix"])
        listed = client.get("/api/auth/keys").json()
        assert listed[0]["prefix"] == created.json()["prefix"]
        assert "secret" not in listed[0]

        client.cookies.clear()
        via_key = client.get("/api/sessions", headers={"X-API-Key": secret})
        assert [row["id"] for row in via_key.json()] == ["personal-chat"]
        assert client.get("/api/auth/me", headers={"X-API-Key": secret}).json()["is_admin"] is False

        # A user key cannot create users or a key for someone else.
        denied = client.post(
            "/api/users",
            json={"username": "work", "password": "work-pass-1"},
            headers={"X-API-Key": secret},
        )
        assert denied.status_code == 403
        stolen = client.post("/api/auth/keys", json={"name": "nope", "user_id": ADMIN_USER_ID}, headers={"X-API-Key": secret})
        assert stolen.status_code == 403

        work = client.post(
            "/api/users",
            json={"username": "work", "password": "work-pass-1"},
            headers={"Authorization": "Bearer deploy-key"},
        )
        assert work.status_code == 201
        assert work.json()["is_admin"] is False
        issued = client.post(
            "/api/auth/keys",
            json={"name": "script", "user_id": work.json()["id"]},
            headers={"Authorization": "Bearer deploy-key"},
        )
        assert issued.status_code == 201
        who = client.get("/api/auth/me", headers={"X-API-Key": issued.json()["secret"]})
        assert who.json()["id"] == work.json()["id"]
        assert who.json()["is_admin"] is False
        assert client.get("/api/sessions", headers={"X-API-Key": issued.json()["secret"]}).json() == []
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_internal_token_cannot_pick_a_session(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    db.create_session("admin-chat", title="Admin")
    previous_db = api_module._db
    app = _app(db)
    body = {"msg": {"type": "result"}, "role": "assistant", "msg_type": "result", "content": "hi"}
    try:
        client = TestClient(app)
        missing = client.post("/internal/sessions/admin-chat/event", json=body)
        assert missing.status_code == 404

        wrong = client.post(
            "/internal/sessions/admin-chat/event",
            json=body,
            headers={"X-AgentForge-User": personal.id},
        )
        assert wrong.status_code == 404
        assert db.get_messages("admin-chat") == []

        ok = client.post(
            "/internal/sessions/admin-chat/event",
            json=body,
            headers={"X-AgentForge-User": ADMIN_USER_ID},
        )
        assert ok.status_code == 200
        assert db.get_messages("admin-chat")[0].content == "hi"
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_auth_off_skips_the_wall(tmp_path, monkeypatch):
    monkeypatch.setattr("app.security.settings.security.api_keys", [])
    monkeypatch.delenv("AGENTFORGE_ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    db.create_session("admin-chat")
    token = set_current_user(personal.id)
    try:
        db.create_session("personal-chat")
    finally:
        reset_current_user(token)

    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        me = client.get("/api/auth/me")
        assert me.status_code == 200
        assert me.json() == {"auth_required": False}
        ids = {row["id"] for row in client.get("/api/sessions").json()}
        assert ids == {"admin-chat", "personal-chat"}
        event = client.post(
            "/internal/sessions/admin-chat/event",
            json={"msg": {"type": "result"}, "content": "open"},
        )
        assert event.status_code == 200
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_admin_username_env_is_the_login_name(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_ADMIN_USERNAME", "Ops-User")
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    apply_admin_username(db)
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    assert db.get_user(ADMIN_USER_ID).username == "ops-user"

    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        old = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
        assert old.status_code == 401
        logged_in = client.post("/api/auth/login", json={"username": "ops-user", "password": "admin-pass-1"})
        assert logged_in.status_code == 200
        assert logged_in.json()["username"] == "ops-user"
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_admin_username_env_renames_and_rejects_a_bad_or_taken_name(tmp_path, monkeypatch):
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    assert db.get_user(ADMIN_USER_ID).username == "admin"
    db.create_user("taken-name", hash_password("taken-pass-1"))

    monkeypatch.setenv("AGENTFORGE_ADMIN_USERNAME", "taken-name")
    try:
        apply_admin_username(db)
    except ValueError as exc:
        assert "username" in str(exc)
    else:
        raise AssertionError("admin rename collided with an existing user")
    assert db.get_user(ADMIN_USER_ID).username == "admin"

    monkeypatch.setenv("AGENTFORGE_ADMIN_USERNAME", "not an email")
    try:
        apply_admin_username(db)
    except ValueError as exc:
        assert "AGENTFORGE_ADMIN_USERNAME" in str(exc)
    else:
        raise AssertionError("invalid admin username was accepted")
    assert db.get_user(ADMIN_USER_ID).username == "admin"

    monkeypatch.setenv("AGENTFORGE_ADMIN_USERNAME", "ops-user")
    apply_admin_username(db)
    assert db.get_user(ADMIN_USER_ID).username == "ops-user"


def test_yaml_script_key_and_admin_password_are_separate(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings.security, "api_keys", ["yaml-key"])
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "")
    monkeypatch.setenv("AGENTFORGE_ADMIN_PASSWORD", "admin-pass-1")
    monkeypatch.setenv("AGENTFORGE_ADMIN_USERNAME", "ops-user")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    apply_admin_username(db)
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    db.create_session("admin-chat")

    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        assert client.get("/api/auth/me").status_code == 401
        logged_in = client.post("/api/auth/login", json={"username": "ops-user", "password": "admin-pass-1"})
        assert logged_in.status_code == 200
        client.cookies.clear()
        as_script = client.get("/api/sessions", headers={"X-API-Key": "yaml-key"})
        assert as_script.status_code == 200
        assert [row["id"] for row in as_script.json()] == ["admin-chat"]
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_admin_edits_and_deletes_other_users(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    db.create_session("admin-chat")
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    other = db.create_user("other", hash_password("other-pass-1"))
    token = set_current_user(personal.id)
    try:
        db.create_session("personal-chat")
        db.upsert_fact("preference", "editor", "vim")
    finally:
        reset_current_user(token)

    headers = {"Authorization": "Bearer deploy-key"}
    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        blocked = client.patch(
            f"/api/users/{ADMIN_USER_ID}",
            json={"username": "nope"},
            headers=headers,
        )
        assert blocked.status_code == 403
        assert client.delete(f"/api/users/{ADMIN_USER_ID}", headers=headers).status_code == 403

        renamed = client.patch(
            f"/api/users/{personal.id}",
            json={"username": "work", "password": "work-pass-1"},
            headers=headers,
        )
        assert renamed.status_code == 200
        assert renamed.json()["username"] == "work"
        assert renamed.json()["is_admin"] is False
        taken = client.patch(
            f"/api/users/{personal.id}",
            json={"username": "other"},
            headers=headers,
        )
        assert taken.status_code == 409

        logged_in = client.post("/api/auth/login", json={"username": "work", "password": "work-pass-1"})
        assert logged_in.status_code == 200
        assert client.patch(f"/api/users/{other.id}", json={"username": "nope-2"}).status_code == 403

        client.cookies.clear()
        removed = client.delete(f"/api/users/{personal.id}", headers=headers)
        assert removed.status_code == 200
        assert db.get_user(personal.id) is None
        assert db.get_session("personal-chat") is None
        assert db.get_all_facts() == []
        assert db.get_session("admin-chat") is not None
        assert db.get_user(other.id) is not None
        assert db.get_user(ADMIN_USER_ID) is not None
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_admin_impersonates_a_user_and_can_return(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    other = db.create_user("other", hash_password("other-pass-1"))
    db.create_session("admin-chat", title="Admin")
    token = set_current_user(personal.id)
    try:
        db.create_session("personal-chat", title="Personal")
    finally:
        reset_current_user(token)

    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        logged_in = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
        assert logged_in.status_code == 200
        admin_cookie = client.cookies["af_session"]

        refused = client.post(
            "/api/auth/impersonate",
            json={"user_id": personal.id},
            headers={"Authorization": "Bearer deploy-key"},
        )
        assert refused.status_code == 403
        assert client.cookies["af_session"] == admin_cookie

        blocked = client.post("/api/auth/impersonate", json={"user_id": ADMIN_USER_ID})
        assert blocked.status_code == 403

        started = client.post("/api/auth/impersonate", json={"user_id": personal.id})
        assert started.status_code == 200
        assert started.json()["id"] == personal.id
        assert started.json()["is_admin"] is False
        assert started.json()["impersonator"]["id"] == ADMIN_USER_ID
        assert started.json()["impersonator"]["username"] == "admin"
        assert client.cookies["af_session"] != admin_cookie

        me = client.get("/api/auth/me").json()
        assert me["id"] == personal.id
        assert me["impersonator"]["username"] == "admin"
        assert [row["id"] for row in client.get("/api/sessions").json()] == ["personal-chat"]

        stale = TestClient(app)
        stale.cookies.set("af_session", admin_cookie)
        assert stale.get("/api/auth/me").status_code == 401

        nested = client.post("/api/auth/impersonate", json={"user_id": other.id})
        assert nested.status_code == 403
        assert client.get("/api/auth/me").json()["id"] == personal.id

        client.post("/api/auth/logout")
        assert client.get("/api/auth/me").status_code == 401

        again = client.post("/api/auth/login", json={"username": "admin", "password": "admin-pass-1"})
        assert again.status_code == 200
        client.post("/api/auth/impersonate", json={"user_id": personal.id})
        stopped = client.post("/api/auth/impersonate/stop")
        assert stopped.status_code == 200
        assert stopped.json()["id"] == ADMIN_USER_ID
        assert "impersonator" not in stopped.json()
        assert "impersonator" not in client.get("/api/auth/me").json()
        assert [row["id"] for row in client.get("/api/sessions").json()] == ["admin-chat"]
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_a_normal_user_cannot_impersonate(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTFORGE_API_KEYS", "deploy-key")
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    db.set_user_password(ADMIN_USER_ID, hash_password("admin-pass-1"))
    personal = db.create_user("personal", hash_password("personal-pass-1"))
    other = db.create_user("other", hash_password("other-pass-1"))
    previous_db = api_module._db
    app = _app(db)
    try:
        client = TestClient(app)
        logged_in = client.post("/api/auth/login", json={"username": "personal", "password": "personal-pass-1"})
        assert logged_in.status_code == 200
        denied = client.post("/api/auth/impersonate", json={"user_id": other.id})
        assert denied.status_code == 403
        missing = client.post("/api/auth/impersonate", json={"user_id": "no-such-user"})
        assert missing.status_code == 403
        assert client.post("/api/auth/impersonate/stop").status_code == 403
        assert client.get("/api/auth/me").json()["id"] == personal.id
    finally:
        set_auth_database(None)
        api_module.set_database(previous_db)


def test_upgrade_from_007_adds_impersonator_column(tmp_path):
    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('007_user_resources')"))
            conn.execute(
                text(
                    "CREATE TABLE users ("
                    "id VARCHAR PRIMARY KEY, username VARCHAR NOT NULL, password_hash VARCHAR NOT NULL, "
                    "is_admin BOOLEAN NOT NULL, created_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE auth_sessions ("
                    "id VARCHAR PRIMARY KEY, user_id VARCHAR NOT NULL, token_hash VARCHAR NOT NULL, "
                    "created_at DATETIME NOT NULL, expires_at DATETIME NOT NULL, revoked_at DATETIME)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO auth_sessions (id, user_id, token_hash, created_at, expires_at) "
                    "VALUES ('s1', :uid, 'hash', datetime('now'), datetime('now', '+1 day'))"
                ),
                {"uid": ADMIN_USER_ID},
            )
    finally:
        engine.dispose()

    upgrade(db_path, database="chat")
    assert current(db_path, database="chat") == "008_impersonation"

    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            owner = conn.execute(text("SELECT impersonator_user_id FROM auth_sessions WHERE id = 's1'")).scalar()
            assert owner is None
    finally:
        engine.dispose()
