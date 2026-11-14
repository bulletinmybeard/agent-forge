"""Owner columns for connections, scheduled jobs, and monitors.

Revision ID: 007_user_resources
Revises: 006_users
Create Date: 2026-11-05

Existing rows belong to the admin user. A database created from the current
models already has ``user_id`` (baseline ``create_all`` follows the models).
A database stamped at 006 does not.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "007_user_resources"
down_revision: Union[str, Sequence[str], None] = "006_users"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Keep in sync with web.server.identity.ADMIN_USER_ID.
_ADMIN_USER_ID = "00000000-0000-0000-0000-000000000001"
_TABLES = ("connections", "scheduled_jobs", "monitor_jobs")


def _inspector() -> sa.Inspector:
    inspector = sa.inspect(op.get_bind())
    inspector.clear_cache()
    return inspector


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        _add_owner(bind, table)


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


def downgrade() -> None:
    for table in _TABLES:
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
