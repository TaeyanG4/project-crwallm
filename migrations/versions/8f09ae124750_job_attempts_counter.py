"""job attempts counter

Revision ID: 8f09ae124750
Revises: 0001
Create Date: 2026-09-03 00:44:09.387901
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "8f09ae124750"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # A server default, which autogenerate does not add. Without one this
    # fails on any database that already has jobs in it: the new column is
    # NOT NULL and every existing row would need a value.
    op.add_column(
        "crawl_jobs",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("crawl_jobs", "attempts")
