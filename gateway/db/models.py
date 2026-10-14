import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ApiKeyRow(Base):
    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16))  # for display only, e.g. "gw-AbC1"
    rpm_limit: Mapped[int | None] = mapped_column(Integer)
    tpm_limit: Mapped[int | None] = mapped_column(Integer)
    monthly_spend_cap_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6))
    allowed_models: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    routing_quality: Mapped[str] = mapped_column(String(16), default="balanced")
    distill_opt_in: Mapped[bool] = mapped_column(Boolean, default=False)
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)
    distill_serving: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RequestRow(Base):
    __tablename__ = "requests"
    __table_args__ = (
        Index("ix_requests_key_created", "key_id", "created_at"),
        Index("ix_requests_created", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    key_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("api_keys.id"))
    alias: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    upstream_model: Mapped[str] = mapped_column(String(200))
    stream: Mapped[bool] = mapped_column(Boolean)
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    usage_estimated: Mapped[bool] = mapped_column(Boolean)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    latency_ms: Mapped[int] = mapped_column(Integer)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    error_type: Mapped[str | None] = mapped_column(String(64))
    fallback_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    route: Mapped[str | None] = mapped_column(String(16))
    route_probability: Mapped[float | None] = mapped_column(Float)


class CaptureEventRow(Base):
    """Metadata for one captured request (topic gateway.events.v1). Message content lives in
    Parquet (`parquet_path`), not here, so this table stays small and fast to aggregate."""

    __tablename__ = "capture_events"
    __table_args__ = (
        Index("ix_capture_events_ts", "ts"),
        Index("ix_capture_events_system_fp", "system_fingerprint"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)  # = request id; replays dedupe on it
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    key_id: Mapped[uuid.UUID | None] = mapped_column()
    simulated: Mapped[bool] = mapped_column(Boolean)
    alias: Mapped[str] = mapped_column(String(100))
    served_alias: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(50))
    upstream_model: Mapped[str] = mapped_column(String(200))
    system_fingerprint: Mapped[str | None] = mapped_column(String(32))
    template_fingerprint: Mapped[str | None] = mapped_column(String(32))
    content_captured: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(32))
    error_type: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int] = mapped_column(Integer)
    completion_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(18, 10))
    latency_ms: Mapped[int] = mapped_column(Integer)
    cache_hit: Mapped[bool] = mapped_column(Boolean)
    pii_redactions: Mapped[int] = mapped_column(Integer)
    parquet_path: Mapped[str | None] = mapped_column(String(500))
    kafka_partition: Mapped[int] = mapped_column(Integer)
    kafka_offset: Mapped[int] = mapped_column(BigInteger)


class DistillAuditRow(Base):
    """Every rollout change: registration, manual state changes, automatic rollbacks."""

    __tablename__ = "distill_audit"
    __table_args__ = (Index("ix_distill_audit_ts", "ts"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    task: Mapped[str] = mapped_column(String(100))
    system_fingerprint: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(32))
    weight: Mapped[float] = mapped_column(Float)
    actor: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(String(500))
