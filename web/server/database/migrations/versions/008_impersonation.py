"""Admin impersonation marker on WebUI sessions.

Revision ID: 008_impersonation
Revises: 007_user_resources
Create Date: 2026-11-05

A null ``impersonator_user_id`` is a normal login. A fresh database already
has the column (baseline ``create_all`` follows the models). A database
stamped at 007 does not.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "008_impersonation"
down_revision: Union[str, Sequence[str], None] = "007_user_resources"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "auth_sessions"
_COLUMN = "impersonator_user_id"
_INDEX = "ix_auth_sessions_impersonator_user_id"


def _inspector() -> sa.Inspector:
    inspector = sa.inspect(op.get_bind())
    inspector.clear_cache()
    return inspector


def upgrade() -> None:
    bind = op.get_bind()
    inspector = _inspector()
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN not in columns:
        bind.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} VARCHAR(36) REFERENCES users (id)"))
    inspector = _inspector()
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX not in indexes:
        op.create_index(_INDEX, _TABLE, [_COLUMN])


def downgrade() -> None:
    inspector = _inspector()
    if _TABLE not in inspector.get_table_names():
        return
    indexes = {index["name"] for index in inspector.get_indexes(_TABLE)}
    if _INDEX in indexes:
        op.drop_index(_INDEX, table_name=_TABLE)
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
