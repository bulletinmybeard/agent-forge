"""Add command_policy_overrides table (runtime shell/ssh permission overrides).

Revision ID: 003_command_policy
Revises: 002_canvas_items
Create Date: 2026-07-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "003_command_policy"
down_revision: Union[str, Sequence[str], None] = "002_canvas_items"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "command_policy_overrides" in inspector.get_table_names():
        return

    op.create_table(
        "command_policy_overrides",
        sa.Column("tool", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("allowed_commands", sa.JSON(), nullable=False),
        sa.Column("allowed_patterns", sa.JSON(), nullable=False),
        sa.Column("blocked_patterns", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("tool"),
    )


def downgrade() -> None:
    op.drop_table("command_policy_overrides")
