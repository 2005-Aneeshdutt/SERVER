import json
from typing import Any

import httpx
import pytest
import respx

from gateway.core.errors import AuthError, BadRequest, ProviderUnavailable
from gateway.providers.anthropic import AnthropicProvider, build_payload
from tests.helpers import ANTHROPIC_URL, request, spec, sse_response

CLAUDE = spec(provider="anthropic", upstream="claude-test")


@pytest.fixture
def claude(http_client: httpx.AsyncClient) -> AnthropicProvider:
    return AnthropicProvider(
        http_client, "https://api.anthropic.com/v1", "ak-secret", default_max_tokens=256
    )


TOOL = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": "Weather for a city",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    },
}


def test_payload_translation_system_tools_and_tool_results() -> None:
    req = request(
        messages=[
            {"role": "system", "content": "Be brief."},
            {"role": "developer", "content": "Use metric."},
            {"role": "user", "content": "Weather in Paris and Rome?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Paris"}'},
                    },
                    {
                        "id": "t2",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"Rome"}'},
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": "18C"},
            {"role": "tool", "tool_call_id": "t2", "content": "24C"},
        ],
        tools=[TOOL],
        tool_choice="required",
        temperature=1.6,
        top_p=0.9,
        stop="END",
    )
    body = build_payload(req, CLAUDE, 256, stream=False)

    assert body["system"] == "Be brief.\n\nUse metric."
    assert body["max_tokens"] == 256  # default applied
    assert body["temperature"] == 1.0  # clamped to Anthropic's range
    assert "top_p" not in body  # newer models reject both
    assert body["stop_sequences"] == ["END"]
    assert body["tool_choice"] == {"type": "any"}
    assert body["tools"][0]["input_schema"]["properties"]["city"] == {"type": "string"}
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    tool_use = body["messages"][1]["content"]
    assert tool_use[0] == {
        "type": "tool_use",
        "id": "t1",
        "name": "get_weather",
        "input": {"city": "Paris"},
    }
    results = body["messages"][2]["content"]  # consecutive tool results merged into one user turn
    assert [r["tool_use_id"] for r in results] == ["t1", "t2"]


def test_images_and_explicit_max_tokens() -> None:
    req = request(
        max_tokens=50,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "What is this?"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBOR"}},
                    {"type": "image_url", "image_url": {"url": "https://x.test/cat.jpg"}},
                ],
            }
        ],
    )
    body = build_payload(req, CLAUDE, 256, stream=True)
    blocks = body["messages"][0]["content"]
    assert body["max_tokens"] == 50 and body["stream"] is True
    assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "iVBOR"}
    assert blocks[2]["source"] == {"type": "url", "url": "https://x.test/cat.jpg"}


def test_unsupported_features_are_bad_requests() -> None:
    with pytest.raises(BadRequest):
        build_payload(request(n=2), CLAUDE, 256, stream=False)
    with pytest.raises(BadRequest):
        build_payload(request(response_format={"type": "json_object"}), CLAUDE, 256, stream=False)


async def test_complete_translates_response(
    claude: AnthropicProvider, upstream: respx.Router
) -> None:
    native = {
        "id": "msg_1",
        "model": "claude-test",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Checking."},
            {"type": "tool_use", "id": "tu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "usage": {"input_tokens": 20, "cache_read_input_tokens": 100, "output_tokens": 15},
    }
    route = upstream.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=native))
    response = await claude.complete(request(), CLAUDE)

    headers = route.calls.last.request.headers
    assert headers["x-api-key"] == "ak-secret" and headers["anthropic-version"] == "2023-06-01"
    choice = response.choices[0]
    assert choice.finish_reason == "tool_calls"
    assert choice.message.content == "Checking."
    assert choice.message.tool_calls is not None
    assert json.loads(choice.message.tool_calls[0].function.arguments) == {"city": "Paris"}
    assert response.usage is not None and response.usage.prompt_tokens == 120


def stream_events() -> list[dict[str, Any] | str]:
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_9",
                "model": "claude-test",
                "usage": {"input_tokens": 11, "output_tokens": 1},
            },
        },
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hi "}},
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "there"},
        },
        {"type": "content_block_stop", "index": 0},
        {
            "type": "content_block_start",
            "index": 1,
            "content_block": {"type": "tool_use", "id": "tu_1", "name": "f", "input": {}},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": '{"a":'},
        },
        {
            "type": "content_block_delta",
            "index": 1,
            "delta": {"type": "input_json_delta", "partial_json": "1}"},
        },
        {"type": "content_block_stop", "index": 1},
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
            "usage": {"output_tokens": 30},
        },
        {"type": "message_stop"},
    ]


async def test_stream_translates_events(claude: AnthropicProvider, upstream: respx.Router) -> None:
    upstream.post(ANTHROPIC_URL).mock(return_value=sse_response(stream_events(), named=True))
    chunks = [c async for c in claude.stream(request(stream=True), CLAUDE)]

    assert chunks[0].choices[0].delta.role == "assistant"
    assert all(c.id == "msg_9" for c in chunks)
    text = "".join(c.choices[0].delta.content or "" for c in chunks if c.choices)
    assert text == "Hi there"
    tool_deltas = [tc for c in chunks if c.choices for tc in (c.choices[0].delta.tool_calls or [])]
    assert tool_deltas[0].id == "tu_1" and tool_deltas[0].function is not None
    assert tool_deltas[0].function.name == "f"
    assert "".join((d.function.arguments or "") for d in tool_deltas if d.function) == '{"a":1}'
    assert any(c.choices and c.choices[0].finish_reason == "tool_calls" for c in chunks)
    usage = chunks[-1].usage
    assert usage is not None and (usage.prompt_tokens, usage.completion_tokens) == (11, 30)


async def test_overloaded_error_event_mid_stream(
    claude: AnthropicProvider, upstream: respx.Router
) -> None:
    events = [
        *stream_events()[:4],
        {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}},
    ]
    upstream.post(ANTHROPIC_URL).mock(return_value=sse_response(events, named=True))
    with pytest.raises(ProviderUnavailable, match="overloaded_error"):
        _ = [c async for c in claude.stream(request(stream=True), CLAUDE)]


async def test_bad_credentials_are_auth_errors(
    claude: AnthropicProvider, upstream: respx.Router
) -> None:
    upstream.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            401,
            json={
                "type": "error",
                "error": {"type": "authentication_error", "message": "invalid x-api-key"},
            },
        )
    )
    with pytest.raises(AuthError):
        await claude.complete(request(), CLAUDE)
