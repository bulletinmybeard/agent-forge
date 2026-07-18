"""Baseline: full chat database schema (pre-Alembic CREATE + ALTER state).

Revision ID: 001_baseline
Revises:
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from web.server.database.models import Base

revision: str = "001_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Use model metadata so the baseline always matches models.py
    # at the time this revision was authored.
    # Future changes must be new revisions.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
