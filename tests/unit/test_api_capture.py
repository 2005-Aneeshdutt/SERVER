"""Capture through the real chat pipeline, with a fake Kafka sender."""

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx

from gateway.capture.producer import CaptureProducer
from gateway.container import _capture
from gateway.core.auth import hash_key
from gateway.core.config import Settings
from gateway.observability.metrics import CAPTURE_DROPPED
from tests.conftest import API_KEY, Gateway
from tests.helpers import GROQ_URL, openai_chunks, openai_completion, sse_response
from tests.unit.test_capture_producer import FakeSender

BODY: dict[str, Any] = {
    "model": "groq-gpt-oss-20b",
    "messages": [
        {"role": "system", "content": "Extract entities as JSON."},
        {"role": "user", "content": "Mail bob@example.com about Acme"},
    ],
}


@pytest.fixture
async def capture(gateway: Gateway) -> AsyncIterator[FakeSender]:
    sender = FakeSender()
    producer = CaptureProducer(sender, "gateway.events.v1", max_queue=100, flush_s=0)
    await producer.start()
    gateway.container.capture = producer
    yield sender
    gateway.container.capture = None
    await producer.stop()


def opt_in(gateway: Gateway, simulated: bool = False) -> None:
    gateway.keys.records[hash_key(API_KEY)] = replace(
        gateway.key, distill_opt_in=True, simulated=simulated
    )


async def events(gateway: Gateway, sender: FakeSender, count: int = 1) -> list[dict[str, Any]]:
    assert gateway.container.capture is not None
    await gateway.container.capture.stop()  # drains the queue
    await gateway.container.capture.start()
    events = [json.loads(line) for _, _, value in sender.sent for line in value.split(b"\n")]
    assert len(events) == count, events
    return events


def test_capture_is_off_by_default() -> None:
    assert _capture(Settings(_env_file=None)) is None


async def test_non_streaming_response_is_captured_scrubbed(
    gateway: Gateway, upstream: respx.Router, capture: FakeSender
) -> None:
    opt_in(gateway)
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion('{"org": ["Acme"]}'))
    )
    response = await gateway.client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    [event] = await events(gateway, capture)
    assert event["event_id"] == response.headers["x-gateway-request-id"]
    assert event["content_captured"] and not event["simulated"]
    assert event["request"]["messages"][1]["content"] == "Mail [EMAIL] about Acme"
    assert event["response"]["text"] == '{"org": ["Acme"]}'
    assert event["meta"]["served_alias"] == "groq-gpt-oss-20b"


async def test_streamed_response_text_is_captured(
    gateway: Gateway, upstream: respx.Router, capture: FakeSender
) -> None:
    opt_in(gateway)
    upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["Ac", "me"])))
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 200
    [event] = await events(gateway, capture)
    assert event["response"]["text"] == "Acme"
    assert event["request"]["params"]["stream"] is True


async def test_keys_without_opt_in_send_metadata_only(
    gateway: Gateway, upstream: respx.Router, capture: FakeSender
) -> None:
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion("x")))
    await gateway.client.post("/v1/chat/completions", json=BODY)

    [event] = await events(gateway, capture)
    assert not event["content_captured"]
    assert event["request"]["messages"] is None and event["response"]["text"] is None


async def test_simulated_traffic_is_labelled(
    gateway: Gateway, upstream: respx.Router, capture: FakeSender
) -> None:
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion("x")))
    opt_in(gateway, simulated=True)
    await gateway.client.post("/v1/chat/completions", json=BODY)  # key flag
    opt_in(gateway)
    await gateway.client.post(
        "/v1/chat/completions", json=BODY, headers={"X-Gateway-Simulated": "1"}
    )  # header
    assert [e["simulated"] for e in await events(gateway, capture, count=2)] == [True, True]


async def test_a_dead_kafka_never_affects_requests(
    gateway: Gateway, upstream: respx.Router
) -> None:
    producer = CaptureProducer(FakeSender(fail_start=True), "t", max_queue=100, flush_s=0)
    await producer.start()
    gateway.container.capture = producer
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion("x")))
    before = CAPTURE_DROPPED.labels(reason="kafka_unavailable")._value.get()

    responses = [await gateway.client.post("/v1/chat/completions", json=BODY) for _ in range(3)]

    await producer.stop()
    gateway.container.capture = None
    assert [r.status_code for r in responses] == [200, 200, 200]
    assert CAPTURE_DROPPED.labels(reason="kafka_unavailable")._value.get() - before == 3
