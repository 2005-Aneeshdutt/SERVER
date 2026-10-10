"""Request logging off the hot path.

Handlers call `submit()` (non-blocking, never awaits I/O). A background task drains the queue and
inserts in batches: whatever has accumulated since the last insert goes in one round trip, so
batches grow naturally under load and stay at size 1 when idle (no added delay).
If Postgres is unreachable, the writer keeps retrying the current batch with backoff while new
records buffer in the bounded queue; once the queue is full, new records are dropped and
counted. Non-transient errors (bad data) drop the batch at once, so one poison batch can't
block logging.
"""

import asyncio
import logging
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy.exc import InterfaceError, OperationalError

from gateway.observability.metrics import (
    REQUEST_LOG_BACKLOG,
    REQUEST_LOG_RETRIES,
    REQUEST_LOGS_DROPPED,
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RequestRecord:
    id: uuid.UUID
    key_id: uuid.UUID | None
    alias: str
    provider: str
    upstream_model: str
    stream: bool
    prompt_tokens: int
    completion_tokens: int
    usage_estimated: bool
    cost_usd: Decimal
    latency_ms: int
    time_to_first_token_ms: int | None
    cache_hit: bool
    status: str  # ok | error | client_disconnected
    error_type: str | None
    fallback_count: int
    created_at: datetime
    route: str | None = None  # model=auto: "cheap" | "strong"
    route_probability: float | None = None


class RequestLogSink(Protocol):
    async def insert_many(self, records: Sequence[RequestRecord]) -> None: ...


_TRANSIENT = (OSError, TimeoutError, OperationalError, InterfaceError)


class RequestLogWriter:
    def __init__(
        self,
        sink: RequestLogSink,
        max_queue: int,
        batch_size: int,
        retry_base_s: float = 0.5,
        retry_max_s: float = 10.0,
    ) -> None:
        self._sink = sink
        self._batch_size = batch_size
        self._retry_base_s = retry_base_s
        self._retry_max_s = retry_max_s
        self._queue: asyncio.Queue[RequestRecord] = asyncio.Queue(maxsize=max_queue)
        self._task: asyncio.Task[None] | None = None

    def submit(self, record: RequestRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            REQUEST_LOGS_DROPPED.labels(reason="queue_full").inc()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="request-log-writer")

    async def stop(self, drain_timeout_s: float = 5.0) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        except TimeoutError:
            log.warning("request log drain timed out; %d records lost", self._queue.qsize())
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._write(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _write(self, batch: list[RequestRecord]) -> None:
        delay = self._retry_base_s
        while True:
            REQUEST_LOG_BACKLOG.set(self._queue.qsize() + len(batch))
            try:
                await self._sink.insert_many(batch)
                REQUEST_LOG_BACKLOG.set(self._queue.qsize())
                return
            except _TRANSIENT:
                # Database unreachable: hold this batch, keep buffering, try again.
                REQUEST_LOG_RETRIES.inc()
                log.warning("request log DB unavailable; retrying in %.1fs", delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._retry_max_s)
            except Exception:
                log.exception("dropping %d request log records (non-retryable)", len(batch))
                REQUEST_LOGS_DROPPED.labels(reason="db_error").inc(len(batch))
                return
