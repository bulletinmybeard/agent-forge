"""Connections, schema cache, schedules, and monitors belong to one user.

Existing rows backfill onto the admin user. Auth off (no actor) stays unfiltered.
"""

from __future__ import annotations

import fnmatch
import json

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from agentforge.connectors.manager import ConnectionManager
from agentforge.tools import sql_schema_tool as schema
from web.server.database import ChatDatabase
from web.server.database.migrate import upgrade
from web.server.database.models import Connection
from web.server.identity import ADMIN_USER_ID, reset_current_user, set_current_user


class _Registry:
    def get(self, _connector_type):
        return None


class _Tools:
    pass


class _Redis:
    def __init__(self):
        self.store: dict[str, str] = {}

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value):
        self.store[key] = value

    def delete(self, *keys):
        removed = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                removed += 1
        return removed

    def keys(self, pattern):
        return [key for key in self.store if fnmatch.fnmatch(key, pattern)]


def test_upgrade_from_006_assigns_existing_rows_to_admin(tmp_path):
    db_path = tmp_path / "old.db"
    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)"))
            conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('006_users')"))
            conn.execute(
                text(
                    "CREATE TABLE connections ("
                    "id VARCHAR PRIMARY KEY, connector_type VARCHAR NOT NULL, label VARCHAR NOT NULL, "
                    "encrypted_tokens TEXT NOT NULL, status VARCHAR NOT NULL, "
                    "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO connections "
                    "(id, connector_type, label, encrypted_tokens, status, created_at, updated_at) "
                    "VALUES ('c1', 'github', 'gh', 'x', 'active', datetime('now'), datetime('now'))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE scheduled_jobs ("
                    "id VARCHAR PRIMARY KEY, label VARCHAR NOT NULL, command TEXT NOT NULL, "
                    "cron VARCHAR NOT NULL, enabled BOOLEAN NOT NULL, "
                    "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO scheduled_jobs (id, label, command, cron, enabled, created_at, updated_at) "
                    "VALUES ('j1', 'nightly', 'echo hi', '0 0 * * *', 1, datetime('now'), datetime('now'))"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE monitor_jobs ("
                    "id VARCHAR PRIMARY KEY, label VARCHAR NOT NULL, url VARCHAR NOT NULL, "
                    "cron VARCHAR NOT NULL, enabled BOOLEAN NOT NULL, "
                    "created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL)"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO monitor_jobs (id, label, url, cron, enabled, created_at, updated_at) "
                    "VALUES ('m1', 'page', 'https://example.test', '0 * * * *', 1, datetime('now'), datetime('now'))"
                )
            )
    finally:
        engine.dispose()

    upgrade(db_path, database="chat")

    engine = create_engine(f"sqlite:///{db_path}", poolclass=NullPool)
    try:
        with engine.connect() as conn:
            assert conn.execute(text("SELECT user_id FROM connections WHERE id = 'c1'")).scalar() == ADMIN_USER_ID
            assert conn.execute(text("SELECT user_id FROM scheduled_jobs WHERE id = 'j1'")).scalar() == ADMIN_USER_ID
            assert conn.execute(text("SELECT user_id FROM monitor_jobs WHERE id = 'm1'")).scalar() == ADMIN_USER_ID
    finally:
        engine.dispose()


def test_jobs_and_connections_are_hidden_from_the_other_user(tmp_path):
    db = ChatDatabase(tmp_path / "chat.db")
    db.create_tables()
    other = db.create_user("other-user", "not-a-hash")

    db.create_scheduled_job("job-admin", "admin job", "echo admin", "0 0 * * *")
    db.create_monitor_job("mon-admin", "admin page", "https://example.test/admin", cron="0 * * * *")
    with db.SessionLocal() as session:
        session.add(
            Connection(
                id="conn-admin",
                user_id=ADMIN_USER_ID,
                connector_type="github",
                label="Admin GitHub",
                encrypted_tokens="x",
                status="active",
            )
        )
        session.commit()

    token = set_current_user(other.id)
    try:
        db.create_scheduled_job("job-other", "other job", "echo other", "0 0 * * *")
        db.create_monitor_job("mon-other", "other page", "https://example.test/other", cron="0 * * * *")
        with db.SessionLocal() as session:
            session.add(
                Connection(
                    id="conn-other",
                    user_id=other.id,
                    connector_type="gitlab",
                    label="Other GitLab",
                    encrypted_tokens="x",
                    status="active",
                )
            )
            session.commit()

        assert [job.id for job in db.list_scheduled_jobs()] == ["job-other"]
        assert db.get_scheduled_job("job-admin") is None
        assert db.delete_scheduled_job("job-admin") is False
        assert [job.id for job in db.list_monitor_jobs()] == ["mon-other"]
        assert db.get_monitor_job("mon-admin") is None

        mgr = ConnectionManager(db.SessionLocal, _Registry(), _Tools())
        assert [row["id"] for row in mgr.list_connections()] == ["conn-other"]
        assert mgr.get_connection("conn-admin") is None
        with pytest.raises(ValueError):
            mgr.get_access_token("conn-admin")
        mgr._agents["conn-admin"] = {
            "id": "connector:conn-admin",
            "user_id": ADMIN_USER_ID,
            "aliases": ["@github"],
            "source": "connector",
        }
        mgr._agents["conn-other"] = {
            "id": "connector:conn-other",
            "user_id": other.id,
            "aliases": ["@gitlab"],
            "source": "connector",
        }
        visible = mgr.aliases_for_actor()
        assert set(visible) == {"@gitlab"}
        assert visible["@gitlab"]["user_id"] == other.id
    finally:
        reset_current_user(token)

    assert {job.id for job in db.list_scheduled_jobs()} == {"job-admin", "job-other"}
    assert {row["id"] for row in ConnectionManager(db.SessionLocal, _Registry(), _Tools()).list_connections()} == {
        "conn-admin",
        "conn-other",
    }


def test_schema_cache_is_per_user_and_legacy_keys_belong_to_admin(monkeypatch):
    redis = _Redis()
    monkeypatch.setattr(schema, "_get_redis", lambda: redis)
    legacy = json.dumps({"table_count": 2, "total_columns": 4, "view_count": 0, "_cached_at": "t0"})
    redis.store["schema_cache:companydb"] = legacy

    admin = set_current_user(ADMIN_USER_ID)
    try:
        found = schema.get_cached_schema("companydb")
        assert found["table_count"] == 2
        assert "schema_cache:companydb" not in redis.store
        assert f"schema_cache:{ADMIN_USER_ID}:companydb" in redis.store
        schema._set_cached_schema("lunadb", {"table_count": 9, "total_columns": 1, "view_count": 0})
    finally:
        reset_current_user(admin)

    other = set_current_user("00000000-0000-0000-0000-0000000000aa")
    try:
        assert schema.get_cached_schema("companydb") is None
        assert schema.get_cached_schema("lunadb") is None
        schema._set_cached_schema("companydb", {"table_count": 1, "total_columns": 1, "view_count": 0})
        assert schema.get_cached_schema_metadata("companydb")["table_count"] == 1
        assert schema.clear_all_cached_schemas() == 1
        assert schema.get_cached_schema("companydb") is None
    finally:
        reset_current_user(other)

    admin = set_current_user(ADMIN_USER_ID)
    try:
        assert schema.get_cached_schema("companydb")["table_count"] == 2
        assert schema.get_cached_schema("lunadb")["table_count"] == 9
    finally:
        reset_current_user(admin)
