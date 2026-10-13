import json
from decimal import Decimal
from typing import Any

import httpx
import respx

from gateway.core.auth import hash_key
from tests.conftest import Gateway
from tests.helpers import (
    GROQ_URL,
    OPENAI_URL,
    key_record,
    openai_chunks,
    openai_completion,
    sse_response,
)

BODY = {"model": "groq-gpt-oss-20b", "messages": [{"role": "user", "content": "Hi"}]}
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def only_groq(gateway: Gateway) -> None:
    """Disable fallbacks by leaving Groq as the only configured provider."""
    gateway.container.dispatcher.providers = {"groq": gateway.container.providers["groq"]}


def sse_payloads(text: str) -> list[Any]:
    out: list[Any] = []
    for line in text.splitlines():
        if line.startswith("data: "):
            data = line.removeprefix("data: ")
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


# ---- auth & routing ------------------------------------------------------------------------


async def test_missing_key_is_401_in_openai_format(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/v1/chat/completions", json=BODY, headers={"Authorization": ""}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


async def test_unknown_and_revoked_keys_are_401(gateway: Gateway) -> None:
    gateway.keys.records[hash_key("gw-revoked")] = key_record(active=False)
    for key in ("gw-nope", "gw-revoked", "sk-not-ours"):
        response = await gateway.client.post(
            "/v1/chat/completions", json=BODY, headers={"Authorization": f"Bearer {key}"}
        )
        assert response.status_code == 401, key


async def test_unknown_model_is_404(gateway: Gateway) -> None:
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "model": "gpt-9"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


async def test_model_outside_allowlist_is_403(gateway: Gateway) -> None:
    gateway.keys.records[hash_key("gw-limited")] = key_record(allowed=frozenset({"gemini-flash"}))
    response = await gateway.client.post(
        "/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer gw-limited"}
    )
    assert response.status_code == 403


async def test_invalid_body_is_400_in_openai_format(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/v1/chat/completions", json={"model": "groq-gpt-oss-20b", "messages": []}
    )
    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error" and error["param"] == "messages"


async def test_models_endpoint_lists_configured_allowed_aliases(gateway: Gateway) -> None:
    ids = {m["id"] for m in (await gateway.client.get("/v1/models")).json()["data"]}
    assert {"groq-gpt-oss-20b", "gemini-flash", "claude-haiku", "gpt-4o-mini"} <= ids

    gateway.keys.records[hash_key("gw-limited")] = key_record(allowed=frozenset({"gemini-flash"}))
    limited = await gateway.client.get("/v1/models", headers={"Authorization": "Bearer gw-limited"})
    assert [m["id"] for m in limited.json()["data"]] == ["gemini-flash"]


async def test_healthz_and_metrics(gateway: Gateway) -> None:
    assert (await gateway.client.get("/healthz")).json() == {"status": "ok"}
    assert "gateway_requests_total" in (await gateway.client.get("/metrics")).text


# ---- non-streaming -------------------------------------------------------------------------


async def test_completion_proxies_and_logs_cost(gateway: Gateway, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion(prompt=1000, completion=500))
    )
    response = await gateway.client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "Hello!"
    assert response.headers["x-gateway-provider"] == "groq"
    assert response.headers["x-gateway-model"] == "openai/gpt-oss-20b"
    # 1000 * $0.075/M + 500 * $0.30/M = $0.000225
    assert Decimal(response.headers["x-gateway-cost-usd"]) == Decimal("0.000225")

    [record] = await gateway.logged()
    assert record.status == "ok" and record.key_id == gateway.key.id
    assert (record.prompt_tokens, record.completion_tokens) == (1000, 500)
    assert record.cost_usd == Decimal("0.000225") and not record.usage_estimated
    assert str(record.id) == response.headers["x-gateway-request-id"]
    timing = dict(
        part.strip().split(";dur=") for part in response.headers["server-timing"].split(",")
    )
    assert float(timing["gateway"]) >= 0 and float(timing["upstream"]) > 0


async def test_upstream_rate_limit_is_429_with_retry_after_and_logged(
    gateway: Gateway, upstream: respx.Router
) -> None:
    only_groq(gateway)
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "slow"}}, headers={"retry-after": "4"}
        )
    )
    response = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 429
    assert response.headers["retry-after"] == "4"
    [record] = await gateway.logged()
    assert (record.status, record.error_type) == ("error", "RateLimited")


async def test_no_configured_candidate_is_503(gateway: Gateway) -> None:
    gateway.container.dispatcher.providers = {"anthropic": gateway.container.providers["anthropic"]}
    response = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_provider_available"
    [record] = await gateway.logged()
    assert record.error_type == "NoProviderAvailable"


async def test_missing_usage_is_estimated_and_flagged(
    gateway: Gateway, upstream: respx.Router
) -> None:
    completion = openai_completion("one two three four")
    del completion["usage"]
    upstream.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=completion))
    response = await gateway.client.post(
        "/v1/chat/completions", json={**BODY, "model": "gpt-4o-mini"}
    )
    assert response.json()["usage"]["completion_tokens"] > 0
    [record] = await gateway.logged()
    assert record.usage_estimated


# ---- streaming -----------------------------------------------------------------------------


async def test_stream_proxies_chunks_and_hides_usage_by_default(
    gateway: Gateway, upstream: respx.Router
) -> None:
    upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["Hel", "lo"])))
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    payloads = sse_payloads(response.text)
    assert payloads[-1] == "[DONE]"
    chunks = payloads[:-1]
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "Hello"
    assert all("usage" not in c for c in chunks)

    [record] = await gateway.logged()
    assert record.status == "ok" and record.stream
    assert record.completion_tokens == 2 and not record.usage_estimated
    assert record.time_to_first_token_ms is not None


async def test_stream_include_usage(gateway: Gateway, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["a", "b", "c"])))
    body = {**BODY, "stream": True, "stream_options": {"include_usage": True}}
    payloads = sse_payloads((await gateway.client.post("/v1/chat/completions", json=body)).text)
    usage_chunk = payloads[-2]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}


async def test_stream_without_upstream_usage_is_estimated(
    gateway: Gateway, upstream: respx.Router
) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=sse_response(openai_chunks(["word " * 40], usage=False))
    )
    body = {**BODY, "stream": True, "stream_options": {"include_usage": True}}
    payloads = sse_payloads((await gateway.client.post("/v1/chat/completions", json=body)).text)
    assert payloads[-2]["usage"]["completion_tokens"] > 0
    [record] = await gateway.logged()
    assert record.usage_estimated and record.completion_tokens > 0


async def test_stream_error_before_first_chunk_is_a_real_http_error(
    gateway: Gateway, upstream: respx.Router
) -> None:
    only_groq(gateway)
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "stream": True})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "provider_unavailable"
    [record] = await gateway.logged()
    assert record.error_type == "ProviderUnavailable"


async def test_stream_error_after_chunks_ends_with_error_event(
    gateway: Gateway, upstream: respx.Router
) -> None:
    events = [*openai_chunks(["partial"], usage=False)[:2], {"error": {"message": "boom"}}]
    upstream.post(GROQ_URL).mock(return_value=sse_response(events))
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 200  # headers were already committed
    payloads = sse_payloads(response.text)
    assert payloads[1]["choices"][0]["delta"]["content"] == "partial"
    assert payloads[-1]["error"]["code"] == "provider_unavailable"
    assert "[DONE]" not in payloads
    [record] = await gateway.logged()
    assert (record.status, record.error_type) == ("error", "ProviderUnavailable")
    assert gateway.breaker.failures == ["groq"]  # mid-stream failures count toward the breaker


async def test_client_disconnect_is_logged_and_upstream_closed(gateway: Gateway) -> None:
    """Simulate Starlette abandoning the body iterator when the client goes away."""
    import time
    import uuid
    from collections.abc import AsyncIterator
    from datetime import UTC, datetime

    from gateway.api.chat import CallContext, _sse_events
    from gateway.api.streaming import StreamAccumulator
    from gateway.core.schemas import ChatChunk, ChatRequest, ChunkChoice, Delta
    from gateway.routing.dispatch import DispatchTrace

    closed = False

    async def upstream_chunks() -> AsyncIterator[ChatChunk]:
        nonlocal closed
        try:
            for word in ("a", "b", "c"):
                yield ChatChunk(
                    id="x", created=0, model="m", choices=[ChunkChoice(delta=Delta(content=word))]
                )
        finally:
            closed = True

    spec = gateway.container.registry.get("groq-gpt-oss-20b")
    body = ChatRequest.model_validate({**BODY, "stream": True})
    call = CallContext(
        uuid.uuid4(),
        gateway.key,
        spec.alias,
        DispatchTrace(spec=spec),
        True,
        time.perf_counter(),
        datetime.now(UTC),
        body,
    )
    acc = StreamAccumulator(call.started)
    events = _sse_events(gateway.container, body, call, [], upstream_chunks(), acc)

    await anext(events)
    await events.aclose()  # type: ignore[attr-defined]

    assert closed
    [record] = await gateway.logged()
    assert record.status == "client_disconnected"
    assert record.completion_tokens > 0 and record.usage_estimated


# ---- retries & failover --------------------------------------------------------------------


async def test_failover_to_next_alias_after_retries(
    gateway: Gateway, upstream: respx.Router
) -> None:
    groq = upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(503, json={"error": {"message": "down"}})
    )
    upstream.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("from openrouter", 1000, 0))
    )
    response = await gateway.client.post("/v1/chat/completions", json=BODY)

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from openrouter"
    assert groq.call_count == 2  # retried once, then failed over
    assert response.headers["x-gateway-provider"] == "openrouter"
    assert response.headers["x-gateway-alias"] == "or-llama-8b"
    assert response.headers["x-gateway-fallbacks"] == "1"
    assert response.headers["x-gateway-attempts"] == "3"
    [record] = await gateway.logged()
    assert (record.alias, record.provider, record.fallback_count) == (
        "groq-gpt-oss-20b",
        "openrouter",
        1,
    )
    assert record.cost_usd == Decimal("0.00005")  # priced at the *served* model (1000 * $0.05/M)
    assert gateway.breaker.failures == ["groq", "groq"]
    assert gateway.breaker.successes == ["openrouter"]


async def test_open_circuit_is_skipped_without_calling_upstream(
    gateway: Gateway, upstream: respx.Router
) -> None:
    gateway.breaker.open.add("groq")
    groq = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    upstream.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    response = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 200
    assert not groq.called
    assert response.headers["x-gateway-provider"] == "openrouter"


async def test_client_errors_are_not_failed_over(gateway: Gateway, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad tool schema"}})
    )
    fallback = upstream.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=openai_completion())
    )
    response = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert response.status_code == 400
    assert not fallback.called
    assert gateway.breaker.failures == []  # client mistakes don't trip the breaker


async def test_stream_fails_over_before_first_token(
    gateway: Gateway, upstream: respx.Router
) -> None:
    role_then_error = [*openai_chunks(["x"], usage=False)[:1], {"error": {"message": "overloaded"}}]
    upstream.post(GROQ_URL).mock(return_value=sse_response(role_then_error))
    upstream.post(OPENROUTER_URL).mock(return_value=sse_response(openai_chunks(["Hel", "lo"])))
    response = await gateway.client.post("/v1/chat/completions", json={**BODY, "stream": True})

    assert response.status_code == 200
    assert response.headers["x-gateway-provider"] == "openrouter"
    payloads = sse_payloads(response.text)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in payloads[:-1])
    assert text == "Hello"  # nothing from the failed attempt leaked into the stream
    [record] = await gateway.logged()
    assert (record.provider, record.fallback_count, record.status) == ("openrouter", 1, "ok")


async def test_readyz_reports_dependencies_but_only_fails_when_draining(
    gateway: Gateway,
) -> None:
    async def ok() -> None:
        return None

    async def down() -> None:
        raise ConnectionError("redis down")

    gateway.container.readiness = {"postgres": ok, "redis": down}
    response = await gateway.client.get("/readyz")
    assert response.status_code == 200  # shared dependency down: degraded, still in rotation
    assert response.json() == {
        "ready": True,
        "draining": False,
        "degraded": True,
        "checks": {"postgres": "ok", "redis": "error: ConnectionError"},
    }
    gateway.app.state.draining = True
    assert (await gateway.client.get("/readyz")).status_code == 503
