import asyncio
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from gateway.observability.metrics import REQUEST_LOGS_DROPPED
from gateway.observability.request_log import RequestLogWriter, RequestRecord
from tests.helpers import MemorySink


def record() -> RequestRecord:
    return RequestRecord(
        id=uuid.uuid4(),
        key_id=None,
        alias="a",
        provider="p",
        upstream_model="m",
        stream=False,
        prompt_tokens=1,
        completion_tokens=1,
        usage_estimated=False,
        cost_usd=Decimal(0),
        latency_ms=1,
        time_to_first_token_ms=None,
        cache_hit=False,
        status="ok",
        error_type=None,
        fallback_count=0,
        created_at=datetime.now(UTC),
    )


def dropped(reason: str) -> float:
    value = REQUEST_LOGS_DROPPED.labels(reason=reason)._value.get()
    return float(value)


async def test_records_are_batched_and_flushed_on_stop() -> None:
    sink = MemorySink()
    writer = RequestLogWriter(sink, max_queue=1000, batch_size=50)
    for _ in range(120):
        writer.submit(record())  # queued before the writer task runs -> large batches
    await writer.start()
    await writer.stop()
    assert len(sink.records) == 120
    assert sink.batches == [50, 50, 20]


async def test_submit_never_blocks_and_drops_when_full() -> None:
    sink = MemorySink()
    writer = RequestLogWriter(sink, max_queue=2, batch_size=10)
    before = dropped("queue_full")
    for _ in range(5):
        writer.submit(record())
    assert dropped("queue_full") - before == 3


async def test_sink_failure_drops_batch_and_keeps_running() -> None:
    sink = MemorySink()
    writer = RequestLogWriter(sink, max_queue=100, batch_size=10)
    await writer.start()
    before = dropped("db_error")

    sink.fail = True
    writer.submit(record())
    await asyncio.sleep(0.05)
    sink.fail = False
    writer.submit(record())
    await writer.stop()

    assert dropped("db_error") - before == 1
    assert len(sink.records) == 1


class FlakySink(MemorySink):
    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def insert_many(self, records: Sequence[RequestRecord]) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionRefusedError("postgres down")
        await super().insert_many(records)


async def test_db_outage_buffers_and_retries_instead_of_dropping() -> None:
    sink = FlakySink(failures=3)
    writer = RequestLogWriter(sink, max_queue=100, batch_size=10, retry_base_s=0.01)
    await writer.start()
    before = dropped("db_error")
    for _ in range(5):
        writer.submit(record())
    await writer.stop()
    assert len(sink.records) == 5  # nothing lost: retried until the "DB" came back
    assert dropped("db_error") == before
