"""Add canvas_items table (shared chat SQLite file).

Revision ID: 002_canvas_items
Revises: 001_baseline
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002_canvas_items"
down_revision: Union[str, Sequence[str], None] = "001_baseline"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "canvas_items" in inspector.get_table_names():
        return

    op.create_table(
        "canvas_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("footnote_num", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", "type", "content", name="uq_canvas_session_type_content"),
    )
    op.create_index("canvas_items_session", "canvas_items", ["session_id", "footnote_num"])


def downgrade() -> None:
    op.drop_index("canvas_items_session", table_name="canvas_items")
    op.drop_table("canvas_items")
