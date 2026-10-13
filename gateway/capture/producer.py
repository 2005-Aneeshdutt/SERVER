"""Capture producer: hands request/response events to Kafka without touching request latency.

The hot path calls `submit()`, a non-blocking `put_nowait` into a bounded queue. One background
task then does everything else: PII scrubbing, fingerprints, serialization, and the Kafka send.
Events are sent in batches: up to `max_batch` events (or ~512 KB) per Kafka message, as JSON
lines, flushed every `flush_s`. Per-message overhead in the Kafka client measured ~3.8 ms of
CPU per event when sent one by one under load, which batching amortizes away (ADR 0015).
Fails open: a full queue drops the event, and so does a Kafka outage (with a reconnect attempt
at most every few seconds). Both are counted in metrics. Capture is best-effort by design: no
request ever waits for it or fails because of it. See docs/decisions/0015.
"""

import asyncio
import logging
import time
from typing import Protocol

from gateway.capture.events import (
    CaptureEvent,
    CaptureItem,
    EventMeta,
    EventParams,
    EventPii,
    EventRequest,
    EventResponse,
)
from gateway.capture.fingerprint import system_fingerprint, template_fingerprint
from gateway.capture.pii import Redactions, scrub
from gateway.core.schemas import ChatMessage, ChatRequest, ChatResponse
from gateway.observability.metrics import CAPTURE_BACKLOG, CAPTURE_DROPPED, CAPTURE_SENT

log = logging.getLogger(__name__)


class EventSender(Protocol):
    async def start(self) -> None: ...

    async def send(self, topic: str, key: bytes | None, value: bytes) -> None:
        """Hand one message to the client library; raises if that fails."""
        ...

    async def stop(self) -> None: ...


class CaptureProducer:
    def __init__(
        self,
        sender: EventSender,
        topic: str,
        max_queue: int,
        reconnect_s: float = 5.0,
        send_timeout_s: float = 5.0,
        flush_s: float = 0.2,
        max_batch: int = 200,
        max_message_bytes: int = 512 * 1024,
    ) -> None:
        self._sender = sender
        self._flush_s = flush_s
        self._max_batch = max_batch
        self._max_message_bytes = max_message_bytes
        self._topic = topic
        self._queue: asyncio.Queue[CaptureItem] = asyncio.Queue(maxsize=max_queue)
        self._reconnect_s = reconnect_s
        self._send_timeout_s = send_timeout_s
        self._connected = False
        self._next_connect = 0.0
        self._task: asyncio.Task[None] | None = None

    def submit(self, item: CaptureItem) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            CAPTURE_DROPPED.labels(reason="queue_full").inc()

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="capture-producer")

    async def stop(self, drain_timeout_s: float = 5.0) -> None:
        if self._task is None:
            return
        try:
            await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
        except TimeoutError:
            log.warning("capture drain timed out; %d events lost", self._queue.qsize())
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
        if self._connected:
            await self._sender.stop()  # flushes anything still batched in the client

    async def _run(self) -> None:
        while True:
            batch = [await self._queue.get()]
            if self._queue.qsize() < self._max_batch:
                await asyncio.sleep(self._flush_s)  # let a batch accumulate
            while len(batch) < self._max_batch and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            try:
                await self._handle(batch)
            except Exception:
                log.exception("capture batch dropped")
                CAPTURE_DROPPED.labels(reason="send_error").inc(len(batch))
            finally:
                for _ in batch:
                    self._queue.task_done()
                CAPTURE_BACKLOG.set(self._queue.qsize())

    async def _handle(self, batch: list[CaptureItem]) -> None:
        if not await self._ensure_connected():
            CAPTURE_DROPPED.labels(reason="kafka_unavailable").inc(len(batch))
            return
        lines: list[bytes] = []
        size = 0
        for item in batch:
            line = build_event(item).model_dump_json().encode()
            if lines and size + len(line) > self._max_message_bytes:
                await self._send(lines)
                lines, size = [], 0
            lines.append(line)
            size += len(line) + 1
            await asyncio.sleep(0)  # yield between events: building never blocks requests
        await self._send(lines)

    async def _send(self, lines: list[bytes]) -> None:
        # No key: events are batched across API keys, so per-key ordering in Kafka isn't
        # kept. Nothing downstream needs it (events carry ids and timestamps).
        value = b"\n".join(lines)
        await asyncio.wait_for(self._sender.send(self._topic, None, value), self._send_timeout_s)
        CAPTURE_SENT.inc(len(lines))

    async def _ensure_connected(self) -> bool:
        if self._connected:
            return True
        now = time.monotonic()
        if now < self._next_connect:
            return False
        self._next_connect = now + self._reconnect_s
        try:
            await asyncio.wait_for(self._sender.start(), self._send_timeout_s)
        except Exception as exc:
            log.warning("capture: Kafka unavailable (%s); dropping events", type(exc).__name__)
            return False
        self._connected = True
        return True


def build_event(item: CaptureItem) -> CaptureEvent:
    record, body = item.record, item.body
    pii = Redactions()
    messages = _messages(body.messages, pii) if item.opt_in else None
    text, finish_reason, has_tools = _response_parts(item.response)
    if item.opt_in and text is not None:
        scrubbed = scrub(text)
        pii.add(scrubbed.redactions)
        text = scrubbed.text
    return CaptureEvent(
        event_id=str(record.id),
        ts=record.created_at.isoformat(),
        simulated=item.simulated,
        key_id=str(record.key_id) if record.key_id else None,
        content_captured=item.opt_in,
        request=EventRequest(
            alias=record.alias,
            messages=messages,
            params=_params(body),
            system_fingerprint=system_fingerprint(body.messages),
            template_fingerprint=template_fingerprint(body.messages),
        ),
        response=EventResponse(
            text=text if item.opt_in else None,
            finish_reason=finish_reason,
            has_tool_calls=has_tools,
        ),
        meta=EventMeta(
            served_alias=item.served_alias,
            provider=record.provider,
            upstream_model=record.upstream_model,
            status=record.status,
            error_type=record.error_type,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            cost_usd=format(record.cost_usd, "f"),
            latency_ms=record.latency_ms,
            ttft_ms=record.time_to_first_token_ms,
            cache_hit=record.cache_hit,
            fallback_count=record.fallback_count,
            route=record.route,
        ),
        pii=EventPii(email=pii.email, phone=pii.phone, card=pii.card),
    )


def _messages(messages: list[ChatMessage], pii: Redactions) -> list[dict[str, str]]:
    """Role + scrubbed text only. Images and tool-call payloads are not captured."""
    out = []
    for m in messages:
        content = m.content
        if isinstance(content, list):
            content = " ".join(str(p.get("text", "")) for p in content if p.get("type") == "text")
        scrubbed = scrub(content or "")
        pii.add(scrubbed.redactions)
        out.append({"role": m.role, "content": scrubbed.text})
    return out


def _response_parts(response: ChatResponse | None) -> tuple[str | None, str | None, bool]:
    if response is None or not response.choices:
        return None, None, False
    choice = response.choices[0]
    return choice.message.content, choice.finish_reason, bool(choice.message.tool_calls)


def _params(body: ChatRequest) -> EventParams:
    fmt = body.response_format.get("type") if body.response_format else None
    return EventParams(
        temperature=body.temperature,
        max_tokens=body.output_token_cap(),
        response_format=str(fmt) if fmt else None,
        has_tools=bool(body.tools),
        stream=body.stream,
    )
