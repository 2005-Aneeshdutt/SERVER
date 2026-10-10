"""Needs a migrated Postgres at DATABASE_URL (docker compose up postgres; alembic upgrade head)."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from gateway.core.auth import hash_key
from gateway.core.config import Settings
from gateway.db.models import ApiKeyRow, RequestRow
from gateway.db.repositories import SqlKeyStore, SqlRequestLogSink
from gateway.db.session import make_engine, make_sessionmaker
from gateway.observability.request_log import RequestRecord

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = make_engine(Settings().database_url)
    yield make_sessionmaker(engine)
    await engine.dispose()


async def test_key_roundtrip_stores_only_the_hash(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    store = SqlKeyStore(sessions)
    raw, record = await store.create(
        f"it-{uuid.uuid4().hex[:6]}",
        rpm_limit=60,
        monthly_spend_cap_usd=Decimal("1.50"),
        allowed_models=["groq-llama-8b"],
    )
    try:
        found = await store.get_by_hash(hash_key(raw))
        assert found == record
        assert found.monthly_spend_cap_usd == Decimal("1.500000")
        async with sessions() as session:
            row = await session.scalar(select(ApiKeyRow).where(ApiKeyRow.id == record.id))
            assert row is not None and raw not in (row.key_hash, row.key_prefix)
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(ApiKeyRow).where(ApiKeyRow.id == record.id))


async def test_request_log_batch_insert(sessions: async_sessionmaker[AsyncSession]) -> None:
    ids = [uuid.uuid4() for _ in range(3)]
    records = [
        RequestRecord(
            id=i,
            key_id=None,
            alias="groq-llama-8b",
            provider="groq",
            upstream_model="llama",
            stream=True,
            prompt_tokens=10,
            completion_tokens=5,
            usage_estimated=False,
            cost_usd=Decimal("0.0000009"),
            latency_ms=120,
            time_to_first_token_ms=40,
            cache_hit=False,
            status="ok",
            error_type=None,
            fallback_count=0,
            created_at=datetime.now(UTC),
        )
        for i in ids
    ]
    engine = make_engine(Settings().database_url)
    try:
        await SqlRequestLogSink(engine).insert_many(records)
    finally:
        await engine.dispose()
    try:
        async with sessions() as session:
            rows = (await session.scalars(select(RequestRow).where(RequestRow.id.in_(ids)))).all()
            assert len(rows) == 3
            assert rows[0].cost_usd == Decimal("0.0000009000")
    finally:
        async with sessions() as session, session.begin():
            await session.execute(delete(RequestRow).where(RequestRow.id.in_(ids)))
