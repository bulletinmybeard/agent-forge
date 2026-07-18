"""Baseline: prompt_lab_runs + prompt_lab_results.

Revision ID: 001_pl_baseline
Revises:
Create Date: 2026-07-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from web.server.prompt_lab.database.models import Base

revision: str = "001_pl_baseline"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
