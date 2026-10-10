"""Correctness under concurrency, through 3 gateway instances sharing one real Redis."""

import asyncio
import uuid
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from gateway.core.auth import ApiKeyRecord
from tests.helpers import GROQ_URL, openai_completion
from tests.integration.conftest import Replicas

pytestmark = pytest.mark.integration

BODY = {
    "model": "groq-llama-8b",
    "messages": [{"role": "user", "content": "Hi"}],
    "max_tokens": 100,
}


def key(**limits: Any) -> tuple[str, ApiKeyRecord]:
    record = ApiKeyRecord(
        id=uuid.uuid4(),
        name="it",
        rpm_limit=limits.get("rpm"),
        tpm_limit=limits.get("tpm"),
        monthly_spend_cap_usd=limits.get("cap"),
        allowed_models=None,
        active=True,
    )
    return f"gw-{uuid.uuid4().hex}", record


async def fire(
    clients: list[httpx.AsyncClient], raw_key: str, n: int, body: dict[str, Any] = BODY
) -> list[httpx.Response]:
    headers = {"Authorization": f"Bearer {raw_key}"}
    return await asyncio.gather(
        *(
            clients[i % len(clients)].post("/v1/chat/completions", json=body, headers=headers)
            for i in range(n)
        )
    )


async def test_rpm_limit_holds_across_three_replicas(
    replicas: tuple[Replicas, respx.Router],
) -> None:
    start, upstream = replicas
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    raw, record = key(rpm=10)
    clients = await start(3, [(raw, record)])

    responses = await fire(clients, raw, 60)
    codes = [r.status_code for r in responses]
    assert codes.count(200) == 10  # bucket capacity; refill over ~100ms is < 1 request
    assert codes.count(429) == 50
    rejected = next(r for r in responses if r.status_code == 429)
    assert int(rejected.headers["retry-after"]) >= 1
    assert rejected.headers["x-ratelimit-remaining"] == "0"


async def test_tpm_limit_holds_across_replicas(replicas: tuple[Replicas, respx.Router]) -> None:
    start, upstream = replicas
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    # Each request is charged ~110 tokens up front (prompt estimate + max_tokens=100).
    raw, record = key(tpm=1000)
    clients = await start(3, [(raw, record)])
    codes = [r.status_code for r in await fire(clients, raw, 30)]
    admitted = codes.count(200)
    # Charge per request: prompt estimate (8) + max_tokens (100) = 108; 1000 // 108 = 9.
    assert admitted == 9
    assert codes.count(429) == 30 - admitted


async def test_spend_cap_is_never_overshot_by_parallel_requests(
    replicas: tuple[Replicas, respx.Router],
) -> None:
    start, upstream = replicas

    async def slow_completion(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)  # keep every admitted request in flight at the same time
        return httpx.Response(200, json=openai_completion(prompt=10, completion=100))

    upstream.post(GROQ_URL).mock(side_effect=slow_completion)
    # Reservation per request is the worst case over the fallback chain: gemini-flash-lite,
    # 10 prompt tokens (8 x 1.25 margin) * $0.25/M + 100 output * $1.50/M = $0.0001525.
    # With a $0.001 cap and all requests in flight at once: 0.001 // 0.0001525 = 6.
    raw, record = key(cap=Decimal("0.001"))
    clients = await start(3, [(raw, record)])
    responses = await fire(clients, raw, 40)
    codes = [r.status_code for r in responses]
    admitted = codes.count(200)

    actual = sum(
        Decimal(r.headers["x-gateway-cost-usd"]) for r in responses if r.status_code == 200
    )
    assert admitted == 6
    assert codes.count(402) == 40 - admitted
    assert actual <= Decimal("0.001")  # the invariant: never spend past the cap


async def test_reservations_are_released_so_budget_is_reusable(
    replicas: tuple[Replicas, respx.Router],
) -> None:
    start, upstream = replicas
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion(prompt=10, completion=1))
    )
    raw, record = key(cap=Decimal("0.001"))
    clients = await start(1, [(raw, record)])
    # Sequential requests each reserve ~$0.00015 but settle at ~$0.0000006: far more than
    # 0.001 / 0.00015 requests fit, because each reservation is released on settle.
    codes = [(await fire(clients, raw, 1))[0].status_code for _ in range(20)]
    assert codes == [200] * 20


async def test_100_identical_requests_make_exactly_one_upstream_call(
    replicas: tuple[Replicas, respx.Router],
) -> None:
    start, upstream = replicas
    calls = 0

    async def slow_completion(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.3)
        return httpx.Response(200, json=openai_completion("the one answer"))

    upstream.post(GROQ_URL).mock(side_effect=slow_completion)
    raw, record = key()
    clients = await start(3, [(raw, record)])
    body = {**BODY, "temperature": 0}
    responses = await fire(clients, raw, 100, body)

    assert calls == 1
    assert all(r.status_code == 200 for r in responses)
    assert {r.json()["choices"][0]["message"]["content"] for r in responses} == {"the one answer"}
    caches = [r.headers["x-cache"] for r in responses]
    assert caches.count("MISS") == 1 and caches.count("HIT") == 99
