import logging
import uuid
from collections.abc import Sequence
from dataclasses import fields
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Float, case, cast, func, select, text
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from gateway.core.auth import ApiKeyRecord, generate_key, hash_key
from gateway.db.models import ApiKeyRow, RequestRow
from gateway.observability.metrics import REPLICA_FALLBACKS
from gateway.observability.request_log import RequestRecord

log = logging.getLogger(__name__)
_UNREACHABLE = (OSError, TimeoutError, OperationalError, InterfaceError)


def _to_record(row: ApiKeyRow) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=row.id,
        name=row.name,
        rpm_limit=row.rpm_limit,
        tpm_limit=row.tpm_limit,
        monthly_spend_cap_usd=row.monthly_spend_cap_usd,
        allowed_models=frozenset(row.allowed_models) if row.allowed_models is not None else None,
        active=row.active,
        routing_quality=row.routing_quality,
    )


class SqlKeyStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        async with self._sessions() as session:
            row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.key_hash == key_hash))
            return _to_record(row) if row is not None else None

    async def create(
        self,
        name: str,
        *,
        rpm_limit: int | None = None,
        tpm_limit: int | None = None,
        monthly_spend_cap_usd: Decimal | None = None,
        allowed_models: list[str] | None = None,
        routing_quality: str = "balanced",
    ) -> tuple[str, ApiKeyRecord]:
        """Returns (raw key, record). The raw key is never stored and can't be recovered."""
        raw = generate_key()
        row = ApiKeyRow(
            name=name,
            key_hash=hash_key(raw),
            key_prefix=raw[:7],
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            monthly_spend_cap_usd=monthly_spend_cap_usd,
            allowed_models=allowed_models,
            active=True,
            routing_quality=routing_quality,
        )
        async with self._sessions() as session, session.begin():
            session.add(row)
        return raw, _to_record(row)

    async def list_rows(self) -> list[ApiKeyRow]:
        async with self._sessions() as session:
            return list(await session.scalars(select(ApiKeyRow).order_by(ApiKeyRow.created_at)))

    async def get_row(self, key_id: uuid.UUID) -> ApiKeyRow | None:
        async with self._sessions() as session:
            return await session.get(ApiKeyRow, key_id)

    async def update(self, key_id: uuid.UUID, changes: dict[str, Any]) -> ApiKeyRow | None:
        async with self._sessions() as session, session.begin():
            row = await session.get(ApiKeyRow, key_id)
            if row is None:
                return None
            for name, value in changes.items():
                setattr(row, name, value)
        return row

    async def list_all(self) -> list[tuple[str, ApiKeyRecord]]:
        """(key_prefix, record) pairs, oldest first."""
        async with self._sessions() as session:
            rows = await session.scalars(select(ApiKeyRow).order_by(ApiKeyRow.created_at))
            return [(row.key_prefix, _to_record(row)) for row in rows]


class SqlRequestLogSink:
    """Writes request logs with Postgres COPY (asyncpg's binary bulk-load path).

    Profiling under load showed the ORM bulk insert was one of the largest CPU costs per
    request; COPY skips SQL compilation and per-row parameter handling entirely.
    """

    _COLUMNS = tuple(f.name for f in fields(RequestRecord))

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def insert_many(self, records: Sequence[RequestRecord]) -> None:
        if not records:
            return
        rows = [tuple(getattr(r, c) for c in self._COLUMNS) for r in records]
        async with self._engine.connect() as conn:
            raw = await conn.get_raw_connection()
            asyncpg_conn = raw.driver_connection
            assert asyncpg_conn is not None
            await asyncpg_conn.copy_records_to_table(
                RequestRow.__tablename__, records=rows, columns=self._COLUMNS
            )


class SqlSpendLedger:
    """Settled spend = sum of logged request costs. Always reads the PRIMARY (never a replica):
    a lagging replica would under-count spend and let a key overshoot its cap."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def spent_between(self, key_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
        query = select(func.coalesce(func.sum(RequestRow.cost_usd), 0)).where(
            RequestRow.key_id == key_id,
            RequestRow.created_at >= start,
            RequestRow.created_at < end,
        )
        async with self._sessions() as session:
            return Decimal(await session.scalar(query) or 0)


class SqlUsageReader:
    """Dashboard aggregates. Reads the streaming replica when there is one (slightly stale
    numbers are fine here, unlike spend enforcement) and falls back to the primary if the
    replica is down. See docs/decisions/0011."""

    def __init__(
        self,
        primary: async_sessionmaker[AsyncSession],
        replica: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._primary = primary
        self._replica = replica

    async def usage(
        self, start: datetime, end: datetime, key_id: uuid.UUID | None = None
    ) -> tuple[list[dict[str, Any]], str, float | None]:
        """(rows, source, replica_lag_s). source is "replica" or "primary"."""
        if self._replica is not None:
            try:
                rows = await self._query(self._replica, start, end, key_id)
                return rows, "replica", await self._replica_lag_s()
            except _UNREACHABLE:
                REPLICA_FALLBACKS.inc()
                log.warning("read replica unavailable; usage query falls back to primary")
        return await self._query(self._primary, start, end, key_id), "primary", None

    async def _replica_lag_s(self) -> float | None:
        """Seconds since the last replayed transaction. Overstates lag when the primary is idle
        (nothing to replay), so it's an upper bound, which is what a dashboard banner needs."""
        assert self._replica is not None
        query = text("select extract(epoch from now() - pg_last_xact_replay_timestamp())")
        try:
            async with self._replica() as session:
                lag = await session.scalar(query)
        except _UNREACHABLE:
            return None
        return round(float(lag), 3) if lag is not None else None

    async def _query(
        self,
        sessions: async_sessionmaker[AsyncSession],
        start: datetime,
        end: datetime,
        key_id: uuid.UUID | None,
    ) -> list[dict[str, Any]]:
        r = RequestRow
        query = (
            select(
                r.key_id,
                r.alias,
                func.count().label("requests"),
                func.count().filter(r.status == "error").label("errors"),
                func.count().filter(r.cache_hit).label("cache_hits"),
                func.sum(r.prompt_tokens).label("prompt_tokens"),
                func.sum(r.completion_tokens).label("completion_tokens"),
                func.sum(r.cost_usd).label("cost_usd"),
                func.avg(cast(r.latency_ms, Float)).label("avg_latency_ms"),
                func.percentile_cont(0.5).within_group(r.latency_ms).label("p50_latency_ms"),
                func.percentile_cont(0.95).within_group(r.latency_ms).label("p95_latency_ms"),
                func.avg(r.time_to_first_token_ms).label("avg_ttft_ms"),
                func.sum(case((r.fallback_count > 0, 1), else_=0)).label("fallbacks"),
            )
            .where(r.created_at >= start, r.created_at < end)
            .group_by(r.key_id, r.alias)
            .order_by(func.sum(r.cost_usd).desc())
        )
        if key_id is not None:
            query = query.where(r.key_id == key_id)
        async with sessions() as session:
            rows = (await session.execute(query)).mappings().all()
        return [dict(row) for row in rows]
