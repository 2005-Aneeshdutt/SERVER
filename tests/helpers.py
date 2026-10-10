import json
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import httpx

from gateway.cache.response_cache import CachedEntry
from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import SpendCapExceeded
from gateway.core.schemas import ChatRequest
from gateway.limits.ratelimit import RateDecision
from gateway.limits.spend import Reservation
from gateway.observability.request_log import RequestRecord
from gateway.routing.registry import ModelSpec

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def sse_body(events: Sequence[dict[str, Any] | str], named: bool = False) -> bytes:
    """Encode events as an SSE body. `named=True` adds `event: <type>` lines (Anthropic style)."""
    out: list[str] = []
    for event in events:
        if isinstance(event, str):
            out.append(f"data: {event}\n\n")
            continue
        prefix = f"event: {event['type']}\n" if named else ""
        out.append(f"{prefix}data: {json.dumps(event)}\n\n")
    return "".join(out).encode()


def sse_response(events: Sequence[dict[str, Any] | str], named: bool = False) -> httpx.Response:
    return httpx.Response(
        200, content=sse_body(events, named), headers={"content-type": "text/event-stream"}
    )


def openai_completion(
    content: str = "Hello!", prompt: int = 10, completion: int = 3
) -> dict[str, Any]:
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "llama-3.1-8b-instant",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
        },
        "system_fingerprint": "fp_123",
    }


def openai_chunks(words: Sequence[str], usage: bool = True) -> list[dict[str, Any] | str]:
    base = {
        "id": "chatcmpl-abc",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "m",
    }
    events: list[dict[str, Any] | str] = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}}]}
    ]
    events += [{**base, "choices": [{"index": 0, "delta": {"content": w}}]} for w in words]
    events.append({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
    if usage:
        events.append(
            {
                **base,
                "choices": [],
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": len(words),
                    "total_tokens": 12 + len(words),
                },
            }
        )
    events.append("[DONE]")
    return events


def spec(
    alias: str = "test-model", provider: str = "groq", upstream: str = "up-model"
) -> ModelSpec:
    return ModelSpec(
        alias=alias,
        provider=provider,
        upstream_model=upstream,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("2"),
        context_window=8192,
        tier="cheap",
    )


def request(**overrides: Any) -> ChatRequest:
    data: dict[str, Any] = {"model": "test-model", "messages": [{"role": "user", "content": "Hi"}]}
    data.update(overrides)
    return ChatRequest.model_validate(data)


def key_record(
    allowed: frozenset[str] | None = None, active: bool = True, name: str = "test"
) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=uuid.uuid4(),
        name=name,
        rpm_limit=None,
        tpm_limit=None,
        monthly_spend_cap_usd=None,
        allowed_models=allowed,
        active=active,
    )


class FakeKeyStore:
    def __init__(self) -> None:
        self.records: dict[str, ApiKeyRecord] = {}
        self.lookups = 0

    async def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        self.lookups += 1
        return self.records.get(key_hash)


class MemorySink:
    def __init__(self) -> None:
        self.records: list[RequestRecord] = []
        self.batches: list[int] = []
        self.fail = False

    async def insert_many(self, records: Sequence[RequestRecord]) -> None:
        if self.fail:
            raise RuntimeError("db down")
        self.batches.append(len(records))
        self.records.extend(records)


class FakeBreaker:
    """In-memory breaker: providers in `open` are rejected; calls are recorded."""

    def __init__(self) -> None:
        self.open: set[str] = set()
        self.failures: list[str] = []
        self.successes: list[str] = []

    async def allow(self, provider: str) -> bool:
        return provider not in self.open

    async def record_success(self, provider: str) -> None:
        self.successes.append(provider)

    async def record_failure(self, provider: str) -> bool:
        self.failures.append(provider)
        return provider in self.open


async def no_sleep(_: float) -> None:
    return None


class FakeRateLimiter:
    def __init__(self) -> None:
        self.deny = False
        self.acquired: list[int] = []
        self.adjustments: list[int] = []

    async def acquire(self, key: ApiKeyRecord, tokens: int) -> RateDecision:
        self.acquired.append(tokens)
        if self.deny:
            return RateDecision(False, key.rpm_limit, 0, key.tpm_limit, 0, retry_after_s=7)
        return RateDecision(True, key.rpm_limit, 99 if key.rpm_limit else None)

    async def adjust_tokens(self, key: ApiKeyRecord, delta: int) -> None:
        self.adjustments.append(delta)


class FakeSpend:
    """Same semantics as the Redis limiter, in memory."""

    def __init__(self) -> None:
        self.spent: dict[uuid.UUID, Decimal] = {}
        self.reserved: dict[str, Decimal] = {}
        self.settled: list[tuple[str, Decimal]] = []

    async def reserve(
        self, key: ApiKeyRecord, request_id: str, estimate_usd: Decimal
    ) -> Reservation | None:
        cap = key.monthly_spend_cap_usd
        if cap is None:
            return None
        if self.spent.get(key.id, Decimal(0)) + sum(self.reserved.values()) + estimate_usd > cap:
            raise SpendCapExceeded("cap")
        self.reserved[request_id] = estimate_usd
        return Reservation(key.id, "2026-10", request_id, 0)

    async def settle(self, reservation: Reservation, actual_usd: Decimal) -> None:
        self.reserved.pop(reservation.request_id, None)
        self.spent[reservation.key_id] = self.spent.get(reservation.key_id, Decimal(0)) + actual_usd
        self.settled.append((reservation.request_id, actual_usd))


class MemoryCache:
    def __init__(self) -> None:
        self.entries: dict[str, CachedEntry] = {}
        self.locks: dict[str, str] = {}

    async def get(self, key: str) -> CachedEntry | None:
        return self.entries.get(key)

    async def set(self, key: str, entry: CachedEntry) -> None:
        self.entries[key] = entry

    async def try_lock(self, key: str) -> str | None:
        if key in self.locks:
            return None
        self.locks[key] = token = uuid.uuid4().hex
        return token

    async def unlock(self, key: str, token: str) -> None:
        if self.locks.get(key) == token:
            del self.locks[key]
