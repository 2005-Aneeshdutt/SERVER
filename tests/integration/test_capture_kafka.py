"""Capture end to end on real infrastructure: producer -> Kafka -> consumer -> Postgres + Parquet.

Needs Kafka on localhost:9094 (`docker compose --profile distill up -d kafka`; CI runs a
service container) and the migrated Postgres.
"""

import uuid
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from sqlalchemy import func, select

from distill.consumer import run
from gateway.capture.kafka import KafkaSender
from gateway.capture.producer import CaptureProducer
from gateway.core.config import Settings
from gateway.db.models import CaptureEventRow
from gateway.db.session import make_engine
from tests.unit.test_capture_producer import item

pytestmark = pytest.mark.integration


def parquet_files(root: Path) -> list[Path]:
    return sorted(root.rglob("*.parquet"))


async def produce(settings: Settings, opted_in: int, opted_out: int) -> list[str]:
    producer = CaptureProducer(
        KafkaSender(settings.kafka_bootstrap), settings.capture_topic, max_queue=100
    )
    await producer.start()
    items = [item(opt_in=True, simulated=True) for _ in range(opted_in)]
    items += [item(opt_in=False) for _ in range(opted_out)]
    for captured in items:
        producer.submit(captured)
    await producer.stop()  # drains the queue and flushes the Kafka client
    return [str(i.record.id) for i in items]


async def stored(settings: Settings, ids: list[str]) -> list[CaptureEventRow]:
    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            rows = await conn.execute(
                select(CaptureEventRow).where(CaptureEventRow.id.in_([uuid.UUID(i) for i in ids]))
            )
            return list(rows)  # type: ignore[arg-type]
    finally:
        await engine.dispose()


async def test_events_land_in_postgres_and_parquet_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    settings = Settings(capture_topic=f"test.capture.{uuid.uuid4().hex[:8]}")
    ids = await produce(settings, opted_in=5, opted_out=2)

    new_rows = await run(settings, tmp_path, group_id="test-writer", stop_when_idle=True)

    assert new_rows == 7
    rows = await stored(settings, ids)
    assert len(rows) == 7
    assert sum(r.content_captured for r in rows) == 5
    assert all(r.simulated == r.content_captured for r in rows)  # opted-in items were simulated
    files = parquet_files(tmp_path)
    assert len(files) == 1  # buffered: one file per flush and partition directory
    assert "simulated=true" in files[0].as_posix()
    table = pq.read_table(files[0])
    assert set(table.column("event_id").to_pylist()) <= set(ids)
    assert "[EMAIL]" in table.column("messages_json")[0].as_py()  # scrubbed before Kafka

    # Replay the whole topic from offset 0 with a fresh consumer group: no duplicate rows.
    replayed = await run(settings, tmp_path, group_id="test-replay", stop_when_idle=True)
    assert replayed == 0
    assert len(await stored(settings, ids)) == 7


async def test_metadata_count_matches_events_sent(tmp_path: Path) -> None:
    settings = Settings(capture_topic=f"test.capture.{uuid.uuid4().hex[:8]}")
    ids = await produce(settings, opted_in=0, opted_out=20)
    await run(settings, tmp_path, group_id="test-writer", stop_when_idle=True)

    engine = make_engine(settings.database_url)
    try:
        async with engine.connect() as conn:
            count = await conn.scalar(
                select(func.count())
                .select_from(CaptureEventRow)
                .where(CaptureEventRow.id.in_([uuid.UUID(i) for i in ids]))
            )
    finally:
        await engine.dispose()
    assert count == 20
    assert not parquet_files(tmp_path)  # no opted-in content -> no Parquet
