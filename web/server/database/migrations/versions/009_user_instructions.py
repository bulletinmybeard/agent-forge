"""Per-user model instructions.

Revision ID: 009_user_instructions
Revises: 008_impersonation
Create Date: 2026-11-06

The admin keeps using the ``user_context`` file. Every other user stores
their own text on this column. A fresh database already has it (the model
is the baseline). A database stamped at 008 does not.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009_user_instructions"
down_revision: Union[str, Sequence[str], None] = "008_impersonation"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "users"
_COLUMN = "instructions"


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
    if _COLUMN in columns:
        return
    bind.execute(sa.text(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} TEXT NOT NULL DEFAULT ''"))


def downgrade() -> None:
    inspector = _inspector()
    if _TABLE not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(_TABLE)}
    if _COLUMN in columns:
        op.drop_column(_TABLE, _COLUMN)
