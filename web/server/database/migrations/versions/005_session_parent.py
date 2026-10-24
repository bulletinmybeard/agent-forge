"""Add chat_sessions.parent_session_id.

Revision ID: 005_session_parent
Revises: 004_perm_profiles
Create Date: 2026-10-04
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005_session_parent"
down_revision: Union[str, Sequence[str], None] = "004_perm_profiles"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "parent_session_id" not in columns:
        op.add_column(
            "chat_sessions",
            sa.Column("parent_session_id", sa.String(length=36), nullable=True),
        )
    indexes = {index["name"] for index in inspector.get_indexes("chat_sessions")}
    if "ix_chat_sessions_parent" not in indexes:
        op.create_index("ix_chat_sessions_parent", "chat_sessions", ["parent_session_id"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes("chat_sessions")}
    if "ix_chat_sessions_parent" in indexes:
        op.drop_index("ix_chat_sessions_parent", table_name="chat_sessions")
    columns = {column["name"] for column in inspector.get_columns("chat_sessions")}
    if "parent_session_id" in columns:
        op.drop_column("chat_sessions", "parent_session_id")
