import json
import uuid
from decimal import Decimal
from typing import Any

import httpx
import respx

from gateway.cache.response_cache import (
    cache_key,
    is_cacheable,
    lookup_or_lead,
    replay_chunks,
)
from gateway.core.auth import ApiKeyRecord, hash_key
from gateway.core.schemas import ChatResponse, Choice, FunctionCall, ResponseMessage, ToolCall
from tests.conftest import Gateway
from tests.helpers import (
    GROQ_URL,
    MemoryCache,
    key_record,
    openai_chunks,
    openai_completion,
    request,
    sse_response,
)

BODY: dict[str, Any] = {
    "model": "groq-gpt-oss-20b",
    "messages": [{"role": "user", "content": "Hi"}],
}
CACHED = {**BODY, "temperature": 0}


def with_key(gateway: Gateway, record: ApiKeyRecord, raw: str = "gw-special") -> dict[str, str]:
    gateway.keys.records[hash_key(raw)] = record
    return {"Authorization": f"Bearer {raw}"}


def limited_key(**limits: Any) -> ApiKeyRecord:
    base = key_record()
    return ApiKeyRecord(
        id=base.id,
        name="limited",
        rpm_limit=limits.get("rpm"),
        tpm_limit=limits.get("tpm"),
        monthly_spend_cap_usd=limits.get("cap"),
        allowed_models=None,
        active=True,
    )


# ---- rate limits ---------------------------------------------------------------------------


async def test_rate_limited_key_gets_429_without_calling_upstream(
    gateway: Gateway, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    gateway.rate.deny = True
    headers = with_key(gateway, limited_key(rpm=5, tpm=1000))
    response = await gateway.client.post("/v1/chat/completions", json=BODY, headers=headers)

    assert response.status_code == 429
    assert response.json()["error"]["code"] == "rate_limit_exceeded"
    assert response.headers["retry-after"] == "8"  # rounded up from 7s
    assert response.headers["x-ratelimit-limit"] == "5"
    assert not route.called
    [record] = await gateway.logged()
    assert record.error_type == "RateLimitExceeded"


async def test_tpm_estimate_is_trued_up_after_the_response(
    gateway: Gateway, upstream: respx.Router
) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion(prompt=10, completion=5))
    )
    headers = with_key(gateway, limited_key(tpm=100_000))
    await gateway.client.post(
        "/v1/chat/completions", json={**BODY, "max_tokens": 200}, headers=headers
    )
    [estimate] = gateway.rate.acquired
    assert estimate > 200  # prompt estimate + max_tokens
    assert gateway.rate.adjustments == [estimate - 15]  # refund what wasn't used


# ---- spend limits --------------------------------------------------------------------------


async def test_spend_cap_rejects_with_402_before_calling_upstream(
    gateway: Gateway, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    headers = with_key(gateway, limited_key(cap=Decimal("0.0000001")))
    response = await gateway.client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 402
    assert response.json()["error"]["code"] == "spend_cap_exceeded"
    assert not route.called


async def test_reservation_is_worst_case_and_settled_with_actual_cost(
    gateway: Gateway, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion(prompt=1000, completion=500))
    )
    headers = with_key(gateway, limited_key(cap=Decimal(5)))
    response = await gateway.client.post("/v1/chat/completions", json=BODY, headers=headers)

    assert response.status_code == 200
    # No max_tokens from the client -> the gateway caps output so the reservation is an upper bound.
    assert json.loads(route.calls.last.request.content)["max_tokens"] == 1024
    [(_, actual)] = gateway.spend.settled
    assert actual == Decimal("0.000225")  # 1000 * $0.075/M + 500 * $0.30/M
    assert gateway.spend.reserved == {}


async def test_reservation_covers_the_most_expensive_fallback(
    gateway: Gateway, upstream: respx.Router
) -> None:
    reserved: list[Decimal] = []
    original = gateway.spend.reserve

    async def spy(key: ApiKeyRecord, request_id: str, estimate: Decimal) -> Any:
        reserved.append(estimate)
        return await original(key, request_id, estimate)

    gateway.spend.reserve = spy  # type: ignore[method-assign,assignment]
    # A client error ends the request right after the reservation (no failover on 400).
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(400, json={"error": {"message": "x"}}))
    headers = with_key(gateway, limited_key(cap=Decimal(5)))
    await gateway.client.post(
        "/v1/chat/completions", json={**BODY, "max_tokens": 1_000_000}, headers=headers
    )
    # groq-gpt-oss-20b ($0.08/M out) falls back to gemini-flash-lite ($1.50/M out): reserve for that
    assert reserved and reserved[0] >= Decimal("1.5")


async def test_failed_request_releases_its_reservation(
    gateway: Gateway, upstream: respx.Router
) -> None:
    gateway.container.dispatcher.providers = {"groq": gateway.container.providers["groq"]}
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "x"}}))
    headers = with_key(gateway, limited_key(cap=Decimal(5)))
    response = await gateway.client.post("/v1/chat/completions", json=BODY, headers=headers)
    assert response.status_code == 502
    assert gateway.spend.settled[0][1] == Decimal(0) and gateway.spend.reserved == {}


# ---- cache ---------------------------------------------------------------------------------


async def test_deterministic_request_is_cached_and_hit_costs_nothing(
    gateway: Gateway, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("cached!", 1000, 500))
    )
    first = await gateway.client.post("/v1/chat/completions", json=CACHED)
    second = await gateway.client.post("/v1/chat/completions", json=CACHED)

    assert route.call_count == 1
    assert first.headers["x-cache"] == "MISS" and second.headers["x-cache"] == "HIT"
    assert second.json()["choices"][0]["message"]["content"] == "cached!"
    assert Decimal(second.headers["x-gateway-cost-usd"]) == 0
    miss, hit = await gateway.logged(2)
    assert (miss.cache_hit, hit.cache_hit) == (False, True)
    assert miss.cost_usd > 0 and hit.cost_usd == 0
    assert gateway.cache.locks == {}  # leader released its lock


async def test_cache_hit_replays_as_a_stream(gateway: Gateway, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion("hello")))
    await gateway.client.post("/v1/chat/completions", json=CACHED)
    body = {**CACHED, "stream": True, "stream_options": {"include_usage": True}}
    response = await gateway.client.post("/v1/chat/completions", json=body)

    assert response.headers["x-cache"] == "HIT"
    events = [
        line.removeprefix("data: ")
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c["choices"])
    assert text == "hello"
    assert chunks[-1]["usage"]["total_tokens"] == 13


async def test_streamed_miss_is_cached_for_later_requests(
    gateway: Gateway, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["Hel", "lo"])))
    streamed = await gateway.client.post("/v1/chat/completions", json={**CACHED, "stream": True})
    assert streamed.headers["x-cache"] == "MISS"
    await gateway.logged(1)
    hit = await gateway.client.post("/v1/chat/completions", json=CACHED)
    assert route.call_count == 1
    assert hit.headers["x-cache"] == "HIT"
    assert hit.json()["choices"][0]["message"]["content"] == "Hello"


async def test_cache_modes(gateway: Gateway, upstream: respx.Router) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    sampled = {**BODY, "temperature": 0.7}
    assert (await gateway.client.post("/v1/chat/completions", json=sampled)).headers[
        "x-cache"
    ] == "BYPASS"
    forced = {"X-Gateway-Cache": "force"}
    await gateway.client.post("/v1/chat/completions", json=sampled, headers=forced)
    hit = await gateway.client.post("/v1/chat/completions", json=sampled, headers=forced)
    assert hit.headers["x-cache"] == "HIT"
    skipped = await gateway.client.post(
        "/v1/chat/completions", json=CACHED, headers={"X-Gateway-Cache": "skip"}
    )
    assert skipped.headers["x-cache"] == "BYPASS"
    assert route.call_count == 3


async def test_errors_are_not_cached_and_unlock(gateway: Gateway, upstream: respx.Router) -> None:
    gateway.container.dispatcher.providers = {"groq": gateway.container.providers["groq"]}
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(503, json={"error": {"message": "x"}}))
    await gateway.client.post("/v1/chat/completions", json=CACHED)
    assert gateway.cache.entries == {} and gateway.cache.locks == {}


# ---- cache building blocks -----------------------------------------------------------------


def test_cache_key_covers_output_affecting_fields_only() -> None:
    key_id = uuid.uuid4()
    base = cache_key(key_id, request(temperature=0))
    assert base == cache_key(key_id, request(temperature=0, stream=True, user="u1"))
    assert base != cache_key(key_id, request(temperature=0, max_tokens=5))
    assert base != cache_key(
        key_id, request(temperature=0, tools=[{"type": "function", "function": {"name": "f"}}])
    )
    assert base != cache_key(key_id, request(temperature=0, model="other"))
    assert base != cache_key(uuid.uuid4(), request(temperature=0))  # scoped per API key


def test_cacheability() -> None:
    assert is_cacheable(request(temperature=0), None)
    assert not is_cacheable(request(), None)  # default temperature samples
    assert is_cacheable(request(temperature=1), "force")
    assert not is_cacheable(request(temperature=0), "skip")


async def test_waiter_gets_the_leaders_result() -> None:
    cache = MemoryCache()
    leader = await lookup_or_lead(cache, "k", 5)
    assert leader.lock_token is not None

    async def leader_finishes(_: float) -> None:
        from gateway.cache.response_cache import CachedEntry

        cache.entries["k"] = CachedEntry({"x": 1}, "a", "p", "m", "0", 1)

    waiter = await lookup_or_lead(cache, "k", 5, sleep=leader_finishes)
    assert waiter.entry is not None and waiter.waited and waiter.lock_token is None


async def test_waiter_falls_through_after_timeout() -> None:
    cache = MemoryCache()
    await lookup_or_lead(cache, "k", 5)  # someone holds the lock forever
    now = [0.0]

    async def tick(seconds: float) -> None:
        now[0] += seconds

    result = await lookup_or_lead(cache, "k", 1.0, sleep=tick, clock=lambda: now[0])
    assert result.entry is None and result.lock_token is None and result.waited


def test_replay_includes_tool_calls_and_finish_reason() -> None:
    call = ToolCall(id="c1", function=FunctionCall(name="f", arguments='{"a":1}'))
    response = ChatResponse(
        id="r",
        created=0,
        model="m",
        choices=[Choice(message=ResponseMessage(tool_calls=[call]), finish_reason="tool_calls")],
    )
    chunks = replay_chunks(response)
    deltas = [d for c in chunks for d in (c.choices[0].delta.tool_calls or [])]
    assert deltas[0].id == "c1" and deltas[0].function is not None
    assert deltas[0].function.arguments == '{"a":1}'
    assert chunks[-1].choices[0].finish_reason == "tool_calls"
