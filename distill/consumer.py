"""Capture consumer: Kafka `gateway.events.v1` -> Postgres metadata + Parquet content.

    uv run --group distill python -m distill.consumer

Events are buffered and flushed every `flush_events` events or `flush_s` seconds, so Parquet
files are large (one per flush and partition directory) instead of one tiny file per poll.
At-least-once: offsets are committed only after a flush has written both Postgres and Parquet,
so a crash replays everything since the last flush. That's safe:
- Postgres rows are keyed on the event id (`ON CONFLICT DO NOTHING`).
- Parquet files are named by partition and offset range, so replaying the same batch rewrites
  the same file. Readers (the dataset builder) still dedupe on event_id, because a replay with
  different batch boundaries can write an event twice.
Only events whose key opted in carry content, so only those go to Parquet.
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.capture.events import SCHEMA_VERSION, CaptureEvent
from gateway.core.config import Settings
from gateway.db.models import CaptureEventRow
from gateway.db.session import make_engine

log = logging.getLogger("distill.consumer")
GROUP_ID = "capture-writer"
FLUSH_EVENTS = 5000
FLUSH_S = 60.0

PARQUET_SCHEMA = pa.schema(
    [
        ("event_id", pa.string()),
        ("ts", pa.timestamp("us", tz="UTC")),
        ("simulated", pa.bool_()),
        ("key_id", pa.string()),
        ("alias", pa.string()),
        ("served_alias", pa.string()),
        ("upstream_model", pa.string()),
        ("system_fingerprint", pa.string()),
        ("template_fingerprint", pa.string()),
        ("messages_json", pa.string()),
        ("response_text", pa.string()),
        ("finish_reason", pa.string()),
        ("response_format", pa.string()),
        ("temperature", pa.float64()),
        ("status", pa.string()),
        ("prompt_tokens", pa.int64()),
        ("completion_tokens", pa.int64()),
        ("cost_usd", pa.string()),
        ("latency_ms", pa.int64()),
        ("kafka_partition", pa.int32()),
        ("kafka_offset", pa.int64()),
    ]
)


@dataclass(frozen=True)
class Received:
    event: CaptureEvent
    partition: int
    offset: int


def parse_message(raw: bytes, partition: int, offset: int) -> list[Received]:
    """One Kafka message holds a batch of events as JSON lines (see CaptureProducer)."""
    return [
        received
        for line in raw.split(b"\n")
        if line.strip() and (received := parse(line, partition, offset)) is not None
    ]


def parse(raw: bytes, partition: int, offset: int) -> Received | None:
    """None for events we can't use (bad JSON, unknown schema): logged and skipped, so one
    poison event can't stall the consumer."""
    try:
        data = json.loads(raw)
        if data.get("schema_version") != SCHEMA_VERSION:
            log.warning(
                "skipping offset %d/%d: schema %s", partition, offset, data.get("schema_version")
            )
            return None
        return Received(CaptureEvent.model_validate(data), partition, offset)
    except (ValueError, TypeError):
        log.warning("skipping unparseable message at %d/%d", partition, offset)
        return None


def parquet_rows(batch: Sequence[Received]) -> dict[tuple[str, bool], list[dict[str, Any]]]:
    """Content rows grouped by (date, simulated) partition directory."""
    groups: dict[tuple[str, bool], list[dict[str, Any]]] = defaultdict(list)
    for r in batch:
        e = r.event
        if not e.content_captured:
            continue
        ts = datetime.fromisoformat(e.ts)
        groups[(ts.date().isoformat(), e.simulated)].append(
            {
                "event_id": e.event_id,
                "ts": ts,
                "simulated": e.simulated,
                "key_id": e.key_id,
                "alias": e.request.alias,
                "served_alias": e.meta.served_alias,
                "upstream_model": e.meta.upstream_model,
                "system_fingerprint": e.request.system_fingerprint,
                "template_fingerprint": e.request.template_fingerprint,
                "messages_json": json.dumps(e.request.messages),
                "response_text": e.response.text,
                "finish_reason": e.response.finish_reason,
                "response_format": e.request.params.response_format,
                "temperature": e.request.params.temperature,
                "status": e.meta.status,
                "prompt_tokens": e.meta.prompt_tokens,
                "completion_tokens": e.meta.completion_tokens,
                "cost_usd": e.meta.cost_usd,
                "latency_ms": e.meta.latency_ms,
                "kafka_partition": r.partition,
                "kafka_offset": r.offset,
            }
        )
    return groups


def write_parquet(batch: Sequence[Received], root: Path) -> dict[str, str]:
    """Write content files; returns event_id -> file path (relative to root)."""
    paths: dict[str, str] = {}
    for (day, simulated), rows in parquet_rows(batch).items():
        first, last = rows[0], rows[-1]
        # Several events share one Kafka offset (a batch per message), so the name also
        # carries the first event id: unique per batch, and stable when the batch is replayed.
        name = (
            f"part-p{first['kafka_partition']}-{first['kafka_offset']}-{last['kafka_offset']}"
            f"-{first['event_id'][:8]}.parquet"
        )
        rel = Path(f"date={day}") / f"simulated={str(simulated).lower()}" / name
        (root / rel.parent).mkdir(parents=True, exist_ok=True)
        tmp = root / rel.with_suffix(".tmp")
        pq.write_table(pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA), tmp)
        tmp.replace(root / rel)  # atomic: readers never see a half-written file
        for row in rows:
            paths[row["event_id"]] = rel.as_posix()
    return paths


def metadata_rows(batch: Sequence[Received], paths: dict[str, str]) -> list[dict[str, Any]]:
    rows = []
    for r in batch:
        e = r.event
        redactions = e.pii.email + e.pii.phone + e.pii.card
        rows.append(
            {
                "id": e.event_id,
                "ts": datetime.fromisoformat(e.ts),
                "key_id": e.key_id,
                "simulated": e.simulated,
                "alias": e.request.alias,
                "served_alias": e.meta.served_alias,
                "provider": e.meta.provider,
                "upstream_model": e.meta.upstream_model,
                "system_fingerprint": e.request.system_fingerprint,
                "template_fingerprint": e.request.template_fingerprint,
                "content_captured": e.content_captured,
                "status": e.meta.status,
                "error_type": e.meta.error_type,
                "prompt_tokens": e.meta.prompt_tokens,
                "completion_tokens": e.meta.completion_tokens,
                "cost_usd": Decimal(e.meta.cost_usd),
                "latency_ms": e.meta.latency_ms,
                "cache_hit": e.meta.cache_hit,
                "pii_redactions": redactions,
                "parquet_path": paths.get(e.event_id),
                "kafka_partition": r.partition,
                "kafka_offset": r.offset,
            }
        )
    return rows


async def insert_metadata(engine: AsyncEngine, rows: list[dict[str, Any]]) -> int:
    """Idempotent: a replayed event is ignored. Returns how many rows were new."""
    if not rows:
        return 0
    stmt = insert(CaptureEventRow).on_conflict_do_nothing(index_elements=["id"])
    async with engine.begin() as conn:
        result = await conn.execute(stmt.returning(CaptureEventRow.id), rows)
        return len(result.fetchall())


async def handle_batch(batch: Sequence[Received], engine: AsyncEngine, root: Path) -> int:
    paths = await asyncio.to_thread(write_parquet, batch, root)
    return await insert_metadata(engine, metadata_rows(batch, paths))


async def run(
    settings: Settings,
    root: Path,
    *,
    group_id: str = GROUP_ID,
    stop_when_idle: bool = False,
    flush_events: int = FLUSH_EVENTS,
    flush_s: float = FLUSH_S,
) -> int:
    """Consume until cancelled (or, with stop_when_idle, until the topic is drained)."""
    from aiokafka import AIOKafkaConsumer

    engine = make_engine(settings.database_url)
    consumer = AIOKafkaConsumer(
        settings.capture_topic,
        bootstrap_servers=settings.kafka_bootstrap,
        group_id=group_id,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
    )
    await consumer.start()
    written, idle_polls = 0, 0
    buffer: list[Received] = []
    first_buffered = 0.0

    async def flush() -> None:
        nonlocal written, buffer
        if buffer:
            written += await _write_with_retry(buffer, engine, root)
            log.info("flushed %d events (%d new rows in total)", len(buffer), written)
            buffer = []
        await consumer.commit()  # only now: everything consumed so far is durably written

    try:
        while True:
            fetched = await consumer.getmany(timeout_ms=1000, max_records=500)
            idle_polls = 0 if fetched else idle_polls + 1
            if fetched and not buffer:
                first_buffered = time.monotonic()
            buffer.extend(
                received
                for messages in fetched.values()
                for m in messages
                for received in parse_message(m.value, m.partition, m.offset)
            )
            due = time.monotonic() - first_buffered >= flush_s
            if len(buffer) >= flush_events or (buffer and due):
                await flush()
            # Idle = 5 empty 1 s polls in a row: partition assignment takes a moment, so a
            # single empty poll right after start doesn't mean the topic is drained.
            if stop_when_idle and idle_polls >= 5:
                await flush()
                return written
    finally:
        await consumer.stop()
        await engine.dispose()


async def _write_with_retry(batch: Sequence[Received], engine: AsyncEngine, root: Path) -> int:
    delay = 1.0
    while True:
        try:
            return await handle_batch(batch, engine, root)
        except (OSError, TimeoutError) as exc:
            # Postgres or the disk is unavailable: hold the batch (offsets stay uncommitted).
            log.warning("write failed (%s); retrying in %.0fs", type(exc).__name__, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    root = Path(os.environ.get("CAPTURE_PARQUET_DIR", "data/capture"))
    asyncio.run(run(Settings(), root))


if __name__ == "__main__":
    main()
