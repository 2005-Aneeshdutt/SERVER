import asyncio
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from gateway.capture.events import CaptureItem
from gateway.capture.producer import CaptureProducer, build_event
from gateway.core.schemas import ChatRequest, ChatResponse
from gateway.observability.metrics import CAPTURE_DROPPED, CAPTURE_SENT
from gateway.observability.request_log import RequestRecord


class FakeSender:
    def __init__(self, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = 0
        self.sent: list[tuple[str, bytes | None, bytes]] = []

    async def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise ConnectionError("kafka down")

    async def send(self, topic: str, key: bytes | None, value: bytes) -> None:
        self.sent.append((topic, key, value))

    async def stop(self) -> None:
        pass


def dropped(reason: str) -> float:
    return float(CAPTURE_DROPPED.labels(reason=reason)._value.get())


def item(opt_in: bool = True, simulated: bool = False) -> CaptureItem:
    body = ChatRequest.model_validate(
        {
            "model": "groq-gpt-oss-120b",
            "messages": [
                {"role": "system", "content": "Classify the intent."},
                {"role": "user", "content": "I'm jane@example.com, card 4111 1111 1111 1111"},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    )
    response = ChatResponse.model_validate(
        {
            "id": "c",
            "created": 0,
            "model": "m",
            "choices": [
                {"message": {"content": "call 4155550132"}, "finish_reason": "stop"},
            ],
        }
    )
    record = RequestRecord(
        id=uuid.uuid4(),
        key_id=uuid.uuid4(),
        alias="groq-gpt-oss-120b",
        provider="groq",
        upstream_model="openai/gpt-oss-120b",
        stream=False,
        prompt_tokens=30,
        completion_tokens=5,
        usage_estimated=False,
        cost_usd=Decimal("0.0000075"),
        latency_ms=300,
        time_to_first_token_ms=None,
        cache_hit=False,
        status="ok",
        error_type=None,
        fallback_count=0,
        created_at=datetime.now(UTC),
    )
    return CaptureItem(record, "groq-gpt-oss-120b", body, response, opt_in, simulated)


def test_opted_in_event_carries_scrubbed_content() -> None:
    event = build_event(item(opt_in=True, simulated=True))
    assert event.content_captured and event.simulated
    assert event.request.messages is not None
    user = event.request.messages[1]["content"]
    assert "jane@example.com" not in user and "[EMAIL]" in user and "[CARD]" in user
    assert event.response.text == "call [PHONE]"
    assert (event.pii.email, event.pii.card, event.pii.phone) == (1, 1, 1)
    assert event.request.params.response_format == "json_object"
    assert event.request.system_fingerprint is not None
    assert event.meta.cost_usd == "0.0000075"


def test_opted_out_event_has_metadata_only() -> None:
    event = build_event(item(opt_in=False))
    assert not event.content_captured
    assert event.request.messages is None and event.response.text is None
    assert event.request.system_fingerprint is not None  # fingerprints are hashes, kept
    assert event.meta.prompt_tokens == 30


async def test_events_are_batched_into_json_lines_messages() -> None:
    sender = FakeSender()
    producer = CaptureProducer(sender, "gateway.events.v1", max_queue=10, flush_s=0.05)
    items = [item() for _ in range(3)]
    before = CAPTURE_SENT._value.get()
    for captured in items:
        producer.submit(captured)  # queued before the task starts: one batch
    await producer.start()
    await producer.stop()

    [(topic, key, value)] = sender.sent
    assert topic == "gateway.events.v1" and key is None
    ids = [json.loads(line)["event_id"] for line in value.split(b"\n")]
    assert ids == [str(i.record.id) for i in items]
    assert CAPTURE_SENT._value.get() - before == 3


async def test_large_batches_are_split_by_message_size() -> None:
    sender = FakeSender()
    producer = CaptureProducer(sender, "t", max_queue=50, flush_s=0, max_message_bytes=3000)
    for _ in range(6):
        producer.submit(item())
    await producer.start()
    await producer.stop()
    assert len(sender.sent) > 1
    assert all(len(value) <= 3000 for _, _, value in sender.sent)
    assert sum(len(v.split(b"\n")) for _, _, v in sender.sent) == 6


def test_submit_never_blocks_and_drops_when_full() -> None:
    producer = CaptureProducer(FakeSender(), "t", max_queue=2)  # not started: nothing drains
    before = dropped("queue_full")
    for _ in range(5):
        producer.submit(item())
    assert dropped("queue_full") - before == 3


async def test_kafka_down_drops_events_and_retries_connect_later() -> None:
    sender = FakeSender(fail_start=True)
    producer = CaptureProducer(sender, "t", max_queue=10, reconnect_s=60)
    await producer.start()
    before = dropped("kafka_unavailable")
    for _ in range(3):
        producer.submit(item())
    await asyncio.wait_for(producer._queue.join(), 1)
    await producer.stop()
    assert dropped("kafka_unavailable") - before == 3
    assert sender.started == 1  # one connect attempt, then backoff: no reconnect per event
    assert sender.sent == []
