import json
from typing import Any

import httpx
import pytest
import respx

from gateway.core.errors import AuthError, BadRequest, RateLimited
from gateway.providers.gemini import GeminiProvider, build_payload
from tests.helpers import GEMINI_BASE, request, spec, sse_response

GEMINI = spec(provider="gemini", upstream="gemini-test")
GENERATE_URL = f"{GEMINI_BASE}/gemini-test:generateContent"
STREAM_URL = f"{GEMINI_BASE}/gemini-test:streamGenerateContent"


@pytest.fixture
def gemini(http_client: httpx.AsyncClient) -> GeminiProvider:
    return GeminiProvider(
        http_client, "https://generativelanguage.googleapis.com/v1beta", "g-secret"
    )


def test_payload_translation() -> None:
    req = request(
        messages=[
            {"role": "system", "content": "Be brief."},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Look"},
                    {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/"}},
                ],
            },
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": '{"answer": 42}'},
            {"role": "tool", "tool_call_id": "unknown", "name": "other", "content": "plain text"},
        ],
        temperature=0.2,
        top_p=0.5,
        max_completion_tokens=99,
        stop=["X"],
        seed=1,
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "s", "schema": {"type": "object"}},
        },
        tools=[
            {"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}
        ],
        tool_choice={"type": "function", "function": {"name": "lookup"}},
    )
    body = build_payload(req)

    assert body["systemInstruction"] == {"parts": [{"text": "Be brief."}]}
    assert [c["role"] for c in body["contents"]] == ["user", "model", "user"]
    assert body["contents"][0]["parts"][1] == {
        "inlineData": {"mimeType": "image/jpeg", "data": "/9j/"}
    }
    assert body["contents"][1]["parts"][0] == {
        "functionCall": {"name": "lookup", "args": {"q": "x"}}
    }
    responses = [p["functionResponse"] for p in body["contents"][2]["parts"]]
    assert responses[0] == {
        "name": "lookup",
        "response": {"answer": 42},
    }  # name recovered from call id
    assert responses[1] == {"name": "other", "response": {"result": "plain text"}}
    assert body["generationConfig"] == {
        "temperature": 0.2,
        "topP": 0.5,
        "maxOutputTokens": 99,
        "stopSequences": ["X"],
        "seed": 1,
        "responseMimeType": "application/json",
        "responseJsonSchema": {"type": "object"},
    }
    assert body["tools"][0]["functionDeclarations"][0]["parametersJsonSchema"] == {"type": "object"}
    assert body["toolConfig"] == {
        "functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["lookup"]}
    }


def test_remote_image_urls_are_rejected() -> None:
    req = request(
        messages=[
            {
                "role": "user",
                "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
            }
        ]
    )
    with pytest.raises(BadRequest):
        build_payload(req)


async def test_complete_translates_response(gemini: GeminiProvider, upstream: respx.Router) -> None:
    native = {
        "responseId": "r1",
        "modelVersion": "gemini-test-001",
        "candidates": [
            {
                "index": 0,
                "finishReason": "STOP",
                "content": {
                    "role": "model",
                    "parts": [
                        {"text": "thinking...", "thought": True},
                        {"text": "Answer"},
                        {"functionCall": {"name": "lookup", "args": {"q": "y"}}},
                    ],
                },
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 7,
            "candidatesTokenCount": 5,
            "thoughtsTokenCount": 20,
        },
    }
    route = upstream.post(GENERATE_URL).mock(return_value=httpx.Response(200, json=native))
    response = await gemini.complete(request(), GEMINI)

    assert route.calls.last.request.headers["x-goog-api-key"] == "g-secret"
    choice = response.choices[0]
    assert choice.message.content == "Answer"  # thought parts excluded
    assert choice.finish_reason == "tool_calls"
    assert choice.message.tool_calls is not None
    assert json.loads(choice.message.tool_calls[0].function.arguments) == {"q": "y"}
    assert response.model == "gemini-test-001"
    assert response.usage is not None and response.usage.completion_tokens == 25  # thoughts billed


async def test_blocked_prompt_and_max_tokens(
    gemini: GeminiProvider, upstream: respx.Router
) -> None:
    upstream.post(GENERATE_URL).mock(
        return_value=httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    )
    blocked = await gemini.complete(request(), GEMINI)
    assert blocked.choices[0].finish_reason == "content_filter"

    upstream.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "candidates": [
                    {"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "cut"}]}}
                ]
            },
        )
    )
    truncated = await gemini.complete(request(), GEMINI)
    assert truncated.choices[0].finish_reason == "length"


def stream_events() -> list[dict[str, Any] | str]:
    usage = {"promptTokenCount": 4, "candidatesTokenCount": 1}
    return [
        {
            "candidates": [{"content": {"parts": [{"text": "Hel"}]}}],
            "usageMetadata": usage,
            "modelVersion": "gemini-test",
        },
        {"candidates": [{"content": {"parts": [{"text": "lo"}]}}], "usageMetadata": usage},
        {
            "candidates": [{"content": {"parts": [{"text": "!"}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 4, "candidatesTokenCount": 3},
        },
    ]


async def test_stream_translates_and_reports_final_usage(
    gemini: GeminiProvider, upstream: respx.Router
) -> None:
    route = upstream.post(STREAM_URL).mock(return_value=sse_response(stream_events()))
    chunks = [c async for c in gemini.stream(request(stream=True), GEMINI)]

    assert route.calls.last.request.url.params["alt"] == "sse"
    assert chunks[0].choices[0].delta.role == "assistant"
    assert "".join(c.choices[0].delta.content or "" for c in chunks if c.choices) == "Hello!"
    assert any(c.choices and c.choices[0].finish_reason == "stop" for c in chunks)
    assert not chunks[-1].choices
    assert chunks[-1].usage is not None and chunks[-1].usage.completion_tokens == 3


async def test_stream_function_call(gemini: GeminiProvider, upstream: respx.Router) -> None:
    event = {
        "candidates": [
            {
                "content": {"parts": [{"functionCall": {"name": "f", "args": {"a": 1}}}]},
                "finishReason": "STOP",
            }
        ]
    }
    upstream.post(STREAM_URL).mock(return_value=sse_response([event]))
    chunks = [c async for c in gemini.stream(request(stream=True), GEMINI)]
    calls = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
    assert len(calls) == 1 and calls[0].index == 0 and calls[0].id
    assert chunks[-1].choices[0].finish_reason == "tool_calls"


async def test_errors(gemini: GeminiProvider, upstream: respx.Router) -> None:
    upstream.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            429,
            json={
                "error": {
                    "code": 429,
                    "message": "Resource exhausted",
                    "status": "RESOURCE_EXHAUSTED",
                }
            },
        )
    )
    with pytest.raises(RateLimited):
        await gemini.complete(request(), GEMINI)

    upstream.post(GENERATE_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "error": {
                    "message": "API key not valid.",
                    "details": [{"reason": "API_KEY_INVALID"}],
                }
            },
        )
    )
    with pytest.raises(AuthError):
        await gemini.complete(request(), GEMINI)
