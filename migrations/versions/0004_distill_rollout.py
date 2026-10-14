"""distillation rollout: per-key serving opt-in, audit trail of rollout changes

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("distill_serving", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "distill_audit",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("task", sa.String(100), nullable=False),
        sa.Column("system_fingerprint", sa.String(32), nullable=False),
        sa.Column("action", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
    )
    op.create_index("ix_distill_audit_ts", "distill_audit", ["ts"])


def downgrade() -> None:
    op.drop_table("distill_audit")
    op.drop_column("api_keys", "distill_serving")
