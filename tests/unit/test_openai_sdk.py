"""M1 'done when': the official OpenAI SDK works against the gateway, streaming and not,
for Groq and Gemini. Upstreams are mocked here; scripts/smoke_openai_sdk.py does it for real."""

from typing import Any

import httpx
import httpx2
import openai
import pytest
import respx

from tests.conftest import API_KEY, Gateway
from tests.helpers import GEMINI_BASE, GROQ_URL, openai_chunks, openai_completion, sse_response

GEMINI_COMPLETION = {
    "candidates": [
        {"index": 0, "finishReason": "STOP", "content": {"parts": [{"text": "Hello from Gemini"}]}}
    ],
    "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4},
}
GEMINI_STREAM: list[dict[str, Any] | str] = [
    {"candidates": [{"content": {"parts": [{"text": "Hello "}]}}]},
    {
        "candidates": [{"content": {"parts": [{"text": "from Gemini"}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 4},
    },
]


@pytest.fixture
def sdk(gateway: Gateway) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key=API_KEY,
        base_url="http://gateway/v1",
        # openai>=3 is built on httpx2 (a fork of httpx), so it needs an httpx2 transport.
        http_client=httpx2.AsyncClient(transport=httpx2.ASGITransport(app=gateway.app)),
        max_retries=0,
    )


@pytest.fixture
def mocked(upstream: respx.Router) -> respx.Router:
    def groq(req: httpx.Request) -> httpx.Response:
        if b'"stream":true' in req.content:
            return sse_response(openai_chunks(["Hello", " from", " Groq"]))
        return httpx.Response(200, json=openai_completion("Hello from Groq"))

    upstream.post(GROQ_URL).mock(side_effect=groq)
    upstream.post(f"{GEMINI_BASE}/gemini-2.5-flash:generateContent").mock(
        return_value=httpx.Response(200, json=GEMINI_COMPLETION)
    )
    upstream.post(f"{GEMINI_BASE}/gemini-2.5-flash:streamGenerateContent").mock(
        return_value=sse_response(GEMINI_STREAM)
    )
    return upstream


@pytest.mark.parametrize(
    ("alias", "expected"),
    [("groq-llama-8b", "Hello from Groq"), ("gemini-flash", "Hello from Gemini")],
)
async def test_sdk_non_streaming(
    sdk: openai.AsyncOpenAI, mocked: respx.Router, alias: str, expected: str
) -> None:
    completion = await sdk.chat.completions.create(
        model=alias, messages=[{"role": "user", "content": "Hi"}]
    )
    assert completion.choices[0].message.content == expected
    assert completion.usage is not None and completion.usage.total_tokens > 0


@pytest.mark.parametrize(
    ("alias", "expected"),
    [("groq-llama-8b", "Hello from Groq"), ("gemini-flash", "Hello from Gemini")],
)
async def test_sdk_streaming(
    sdk: openai.AsyncOpenAI, mocked: respx.Router, alias: str, expected: str
) -> None:
    stream = await sdk.chat.completions.create(
        model=alias,
        messages=[{"role": "user", "content": "Hi"}],
        stream=True,
        stream_options={"include_usage": True},
    )
    text, usage = "", None
    async for chunk in stream:
        if chunk.choices:
            text += chunk.choices[0].delta.content or ""
        if chunk.usage:
            usage = chunk.usage
    assert text == expected
    assert usage is not None and usage.completion_tokens > 0


async def test_sdk_surfaces_gateway_errors_as_typed_exceptions(
    sdk: openai.AsyncOpenAI, mocked: respx.Router
) -> None:
    with pytest.raises(openai.NotFoundError):
        await sdk.chat.completions.create(
            model="nope", messages=[{"role": "user", "content": "Hi"}]
        )
    bad = openai.AsyncOpenAI(
        api_key="gw-wrong", base_url="http://gateway/v1", http_client=sdk._client, max_retries=0
    )
    with pytest.raises(openai.AuthenticationError):
        await bad.chat.completions.create(
            model="groq-llama-8b", messages=[{"role": "user", "content": "Hi"}]
        )


async def test_sdk_raises_on_mid_stream_error(
    sdk: openai.AsyncOpenAI, upstream: respx.Router
) -> None:
    events = [*openai_chunks(["partial"], usage=False)[:2], {"error": {"message": "boom"}}]
    upstream.post(GROQ_URL).mock(return_value=sse_response(events))
    stream = await sdk.chat.completions.create(
        model="groq-llama-8b", messages=[{"role": "user", "content": "Hi"}], stream=True
    )
    with pytest.raises(openai.APIError):
        async for _ in stream:
            pass
