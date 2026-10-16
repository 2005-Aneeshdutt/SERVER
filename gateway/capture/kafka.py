"""aiokafka-backed EventSender. Imported only when capture is enabled (the `capture` extra)."""

import asyncio
import logging
from typing import Any

from gateway.observability.metrics import CAPTURE_DROPPED

log = logging.getLogger(__name__)


class KafkaSender:
    def __init__(self, bootstrap: str, client_id: str = "switchyard") -> None:
        self._bootstrap = bootstrap
        self._client_id = client_id
        self._producer: Any = None

    async def start(self) -> None:
        from aiokafka import AIOKafkaProducer

        producer = AIOKafkaProducer(
            bootstrap_servers=self._bootstrap,
            client_id=self._client_id,
            acks=1,  # leader only: capture is best-effort (single broker anyway, ADR 0015)
            linger_ms=20,  # batch small events; nothing waits on delivery
            request_timeout_ms=5000,
        )
        try:
            await producer.start()
        except Exception:
            await producer.stop()
            raise
        self._producer = producer

    async def send(self, topic: str, key: bytes | None, value: bytes) -> None:
        # `send` only appends to the client's batch; delivery completes later. We don't wait
        # for it (that would serialize the background task on broker round trips): failures
        # are counted from the delivery future instead.
        delivery = await self._producer.send(topic, value=value, key=key)
        delivery.add_done_callback(_count_delivery_failure)

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None


def _count_delivery_failure(future: "asyncio.Future[Any]") -> None:
    if future.cancelled() or future.exception() is not None:
        CAPTURE_DROPPED.labels(reason="delivery_error").inc()
