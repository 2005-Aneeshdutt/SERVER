"""distillation capture: per-key opt-in / simulated flags, captured event metadata

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("distill_opt_in", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "api_keys", sa.Column("simulated", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.create_table(
        "capture_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("key_id", postgresql.UUID(as_uuid=True)),
        sa.Column("simulated", sa.Boolean(), nullable=False),
        sa.Column("alias", sa.String(100), nullable=False),
        sa.Column("served_alias", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("upstream_model", sa.String(200), nullable=False),
        sa.Column("system_fingerprint", sa.String(32)),
        sa.Column("template_fingerprint", sa.String(32)),
        sa.Column("content_captured", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error_type", sa.String(64)),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False),
        sa.Column("completion_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(18, 10), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("pii_redactions", sa.Integer(), nullable=False),
        sa.Column("parquet_path", sa.String(500)),
        sa.Column("kafka_partition", sa.Integer(), nullable=False),
        sa.Column("kafka_offset", sa.BigInteger(), nullable=False),
    )
    op.create_index("ix_capture_events_ts", "capture_events", ["ts"])
    op.create_index("ix_capture_events_system_fp", "capture_events", ["system_fingerprint"])


def downgrade() -> None:
    op.drop_table("capture_events")
    op.drop_column("api_keys", "simulated")
    op.drop_column("api_keys", "distill_opt_in")
