import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Index, Integer, Numeric, String, func
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
