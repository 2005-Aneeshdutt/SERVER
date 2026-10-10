import json

import httpx
import pytest
import respx

from gateway.core.errors import BadRequest, ProviderUnavailable, RateLimited
from gateway.providers.groq import GroqProvider
from gateway.providers.openai import OpenAIProvider
from tests.helpers import (
    GROQ_URL,
    OPENAI_URL,
    openai_chunks,
    openai_completion,
    request,
    spec,
    sse_body,
    sse_response,
)


@pytest.fixture
def groq(http_client: httpx.AsyncClient) -> GroqProvider:
    return GroqProvider(http_client, "https://api.groq.com/openai/v1", "groq-secret")


async def test_complete_sends_upstream_model_and_auth(
    groq: GroqProvider, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    response = await groq.complete(
        request(temperature=0, seed=7, logprobs=True), spec(upstream="llama-x")
    )

    sent = json.loads(route.calls.last.request.content)
    assert sent["model"] == "llama-x"
    assert sent["temperature"] == 0
    assert sent["logprobs"] is True  # unknown OpenAI fields pass through
    assert "stream" not in sent
    assert route.calls.last.request.headers["authorization"] == "Bearer groq-secret"
    assert response.choices[0].message.content == "Hello!"
    assert response.usage is not None and response.usage.total_tokens == 13
    assert response.model_extra == {"system_fingerprint": "fp_123"}


async def test_stream_requests_usage_and_yields_chunks(
    groq: GroqProvider, upstream: respx.Router
) -> None:
    route = upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["Hel", "lo"])))
    chunks = [c async for c in groq.stream(request(stream=True), spec())]

    sent = json.loads(route.calls.last.request.content)
    assert sent["stream"] is True and sent["stream_options"] == {"include_usage": True}
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "Hello"
    assert chunks[-1].usage is not None and chunks[-1].usage.completion_tokens == 2


async def test_groq_x_groq_usage_is_normalized(groq: GroqProvider, upstream: respx.Router) -> None:
    final = {
        "id": "c",
        "object": "chat.completion.chunk",
        "created": 1,
        "model": "m",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        "x_groq": {"usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}},
    }
    upstream.post(GROQ_URL).mock(return_value=sse_response([final, "[DONE]"]))
    chunks = [c async for c in groq.stream(request(stream=True), spec())]
    assert chunks[0].usage is not None and chunks[0].usage.completion_tokens == 9
    assert "x_groq" not in (chunks[0].model_extra or {})


async def test_rate_limit_maps_with_retry_after(groq: GroqProvider, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "slow down"}}, headers={"retry-after": "3"}
        )
    )
    with pytest.raises(RateLimited) as info:
        await groq.complete(request(), spec())
    assert info.value.retry_after_s == 3


async def test_stream_error_status_raises_before_first_chunk(
    groq: GroqProvider, upstream: respx.Router
) -> None:
    upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(400, json={"error": {"message": "bad tools"}})
    )
    with pytest.raises(BadRequest, match="bad tools"):
        await anext(groq.stream(request(stream=True), spec()))


async def test_error_event_mid_stream(groq: GroqProvider, upstream: respx.Router) -> None:
    events = [*openai_chunks(["a"], usage=False)[:2], {"error": {"message": "overloaded"}}]
    upstream.post(GROQ_URL).mock(return_value=sse_response(events))
    with pytest.raises(ProviderUnavailable, match="overloaded"):
        _ = [c async for c in groq.stream(request(stream=True), spec())]


async def test_connection_error_is_unavailable(groq: GroqProvider, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(ProviderUnavailable):
        await groq.complete(request(), spec())


async def test_malformed_body_is_unavailable(groq: GroqProvider, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
    with pytest.raises(ProviderUnavailable):
        await groq.complete(request(), spec())


async def test_openai_adapter_uses_its_own_base_url(
    http_client: httpx.AsyncClient, upstream: respx.Router
) -> None:
    route = upstream.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json=openai_completion())
    )
    provider = OpenAIProvider(http_client, "https://api.openai.com/v1", "sk-test")
    await provider.complete(request(), spec(provider="openai"))
    assert route.called
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-test"


async def test_openrouter_adapter(http_client: httpx.AsyncClient, upstream: respx.Router) -> None:
    from gateway.providers.openrouter import OpenRouterProvider

    route = upstream.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            content=b": OPENROUTER PROCESSING\n\n" + sse_body(openai_chunks(["ok"])),
            headers={"content-type": "text/event-stream"},
        )
    )
    provider = OpenRouterProvider(http_client, "https://openrouter.ai/api/v1", "or-key")
    chunks = [c async for c in provider.stream(request(stream=True), spec(provider="openrouter"))]
    assert route.calls.last.request.headers["authorization"] == "Bearer or-key"
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "ok"
