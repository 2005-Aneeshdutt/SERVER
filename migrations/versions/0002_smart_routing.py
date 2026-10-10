"""smart routing: per-key routing quality, routing decision on request logs

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("routing_quality", sa.String(16), nullable=False, server_default="balanced"),
    )
    op.add_column("requests", sa.Column("route", sa.String(16)))
    op.add_column("requests", sa.Column("route_probability", sa.Float()))


def downgrade() -> None:
    op.drop_column("requests", "route_probability")
    op.drop_column("requests", "route")
    op.drop_column("api_keys", "routing_quality")
