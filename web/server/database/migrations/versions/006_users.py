"""Users, per-user API keys, WebUI sessions, and owner columns.

Revision ID: 006_users
Revises: 005_session_parent
Create Date: 2026-11-05

The admin id is fixed. It must match ``web.server.identity.ADMIN_USER_ID``.
Existing chat rows are backfilled onto that user. ``user_facts.key`` stops
being unique for the whole database and becomes unique per user.

Every step checks the inspector. A database created from the current models
already has these tables, and a database stamped at 002 may not have
``user_facts`` yet.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

from web.server.identity import admin_username

revision: str = "006_users"
down_revision: Union[str, Sequence[str], None] = "005_session_parent"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Keep in sync with web.server.identity.ADMIN_USER_ID.
_ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
_OWNER_TABLES = ("chat_sessions", "command_notes", "session_instructions", "user_facts")


def _inspector() -> sa.Inspector:
    inspector = sa.inspect(op.get_bind())
    inspector.clear_cache()
    return inspector


def _has_unique(inspector: sa.Inspector, table: str, columns: set[str]) -> bool:
    found: list[set[str]] = []
    for constraint in inspector.get_unique_constraints(table):
        found.append(set(constraint.get("column_names") or []))
    for index in inspector.get_indexes(table):
        if index.get("unique"):
            found.append(set(index.get("column_names") or []))
    return columns in found


def upgrade() -> None:
    bind = op.get_bind()
    inspector = _inspector()
    tables = set(inspector.get_table_names())

    if "users" not in tables:
        op.create_table(
            "users",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("username", sa.String(length=64), nullable=False),
            sa.Column("password_hash", sa.String(length=256), nullable=False, server_default=""),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("username", name="uq_users_username"),
        )

    bind.execute(
        sa.text(
            "INSERT OR IGNORE INTO users (id, username, password_hash, is_admin, created_at) "
            "VALUES (:id, :username, '', 1, datetime('now'))"
        ),
        {"id": _ADMIN_USER_ID, "username": admin_username()},
    )

    inspector = _inspector()
    tables = set(inspector.get_table_names())
    if "api_keys" not in tables:
        op.create_table(
            "api_keys",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("prefix", sa.String(length=16), nullable=False, index=True),
            sa.Column("key_hash", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("key_hash", name="uq_api_keys_hash"),
        )
    if "auth_sessions" not in set(_inspector().get_table_names()):
        op.create_table(
            "auth_sessions",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("user_id", sa.String(length=36), sa.ForeignKey("users.id"), nullable=False, index=True),
            sa.Column("token_hash", sa.String(length=128), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("token_hash", name="uq_auth_sessions_hash"),
        )

    for table in _OWNER_TABLES:
        _add_owner(bind, table)

    _rebuild_user_facts(bind)


def _add_owner(bind, table: str) -> None:
    inspector = _inspector()
    if table not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table)}
    if "user_id" not in columns:
        bind.execute(
            sa.text(
                f"ALTER TABLE {table} ADD COLUMN user_id VARCHAR(36) NOT NULL "
                f"DEFAULT '{_ADMIN_USER_ID}' REFERENCES users (id)"
            )
        )
    inspector = _inspector()
    indexes = {index["name"] for index in inspector.get_indexes(table)}
    index_name = f"ix_{table}_user_id"
    if index_name not in indexes:
        op.create_index(index_name, table, ["user_id"])


def _rebuild_user_facts(bind) -> None:
    """Drop the global unique on ``key`` and keep ``(user_id, key)``."""
    inspector = _inspector()
    if "user_facts" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("user_facts")}
    if "user_id" not in columns:
        return
    if _has_unique(inspector, "user_facts", {"user_id", "key"}):
        return

    bind.execute(
        sa.text(
            "CREATE TABLE user_facts_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "fact_type VARCHAR(30) NOT NULL, "
            "key VARCHAR(255) NOT NULL, "
            "value TEXT NOT NULL, "
            "source_session VARCHAR(36), "
            "confidence FLOAT NOT NULL, "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, "
            f"user_id VARCHAR(36) NOT NULL DEFAULT '{_ADMIN_USER_ID}' REFERENCES users (id), "
            "CONSTRAINT uq_user_facts_user_key UNIQUE (user_id, key))"
        )
    )
    bind.execute(
        sa.text(
            "INSERT INTO user_facts_new "
            "(id, fact_type, key, value, source_session, confidence, created_at, updated_at, user_id) "
            "SELECT id, fact_type, key, value, source_session, confidence, created_at, updated_at, "
            f"COALESCE(user_id, '{_ADMIN_USER_ID}') FROM user_facts"
        )
    )
    bind.execute(sa.text("DROP TABLE user_facts"))
    bind.execute(sa.text("ALTER TABLE user_facts_new RENAME TO user_facts"))
    bind.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_user_facts_user_id ON user_facts (user_id)"))


def downgrade() -> None:
    for table in _OWNER_TABLES:
        inspector = _inspector()
        if table not in inspector.get_table_names():
            continue
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        index_name = f"ix_{table}_user_id"
        if index_name in indexes:
            op.drop_index(index_name, table_name=table)
        columns = {column["name"] for column in inspector.get_columns(table)}
        if "user_id" in columns:
            op.drop_column(table, "user_id")
    inspector = _inspector()
    tables = set(inspector.get_table_names())
    for table in ("api_keys", "auth_sessions", "users"):
        if table in tables:
            op.drop_table(table)
            tables.discard(table)
