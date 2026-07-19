"""Add command_permission_profiles + active profile singleton.

Revision ID: 004_perm_profiles
Revises: 003_command_policy
Create Date: 2026-07-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004_perm_profiles"
down_revision: Union[str, Sequence[str], None] = "003_command_policy"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "command_permission_profiles" not in tables:
        op.create_table(
            "command_permission_profiles",
            sa.Column("id", sa.String(length=64), nullable=False),
            sa.Column("description", sa.String(length=500), nullable=False),
            sa.Column("shell", sa.JSON(), nullable=True),
            sa.Column("ssh", sa.JSON(), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "command_permission_active" not in tables:
        op.create_table(
            "command_permission_active",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("profile_id", sa.String(length=64), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )


def downgrade() -> None:
    op.drop_table("command_permission_active")
    op.drop_table("command_permission_profiles")
