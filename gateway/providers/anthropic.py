"""Anthropic Messages API adapter (OpenAI format <-> native format)."""

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.core.errors import BadRequest, ProviderUnavailable, RateLimited
from gateway.core.schemas import (
    ChatChunk,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Delta,
    FunctionCall,
    FunctionCallDelta,
    ResponseMessage,
    ToolCall,
    ToolCallDelta,
    Usage,
)
from gateway.providers.base import new_completion_id, now_ts
from gateway.providers.content import (
    content_parts,
    function_tools,
    image_url_of,
    parse_arguments,
    parse_data_url,
    plain_text,
    system_prompt,
)
from gateway.providers.http import iter_sse, open_stream, parse_json_object, post_json
from gateway.routing.registry import ModelSpec

ANTHROPIC_VERSION = "2023-06-01"
NAME = "anthropic"

_FINISH_REASONS = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class AnthropicProvider:
    name = NAME

    def __init__(
        self, client: httpx.AsyncClient, base_url: str, api_key: str, default_max_tokens: int
    ) -> None:
        self._client = client
        self._url = base_url.rstrip("/") + "/messages"
        self._headers = {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
        self._default_max_tokens = default_max_tokens

    async def complete(self, req: ChatRequest, model: ModelSpec) -> ChatResponse:
        payload = build_payload(req, model, self._default_max_tokens, stream=False)
        data = await post_json(self._client, NAME, self._url, self._headers, payload)
        return to_response(data, model)

    async def stream(self, req: ChatRequest, model: ModelSpec) -> AsyncIterator[ChatChunk]:
        payload = build_payload(req, model, self._default_max_tokens, stream=True)
        state = _StreamState(id=new_completion_id(), created=now_ts(), model=model.upstream_model)
        async with open_stream(self._client, NAME, self._url, self._headers, payload) as resp:
            async for _event, data in iter_sse(resp):
                for chunk in state.handle(parse_json_object(NAME, data)):
                    yield chunk


# ---- request translation -------------------------------------------------------------------


def build_payload(
    req: ChatRequest, model: ModelSpec, default_max_tokens: int, *, stream: bool
) -> dict[str, Any]:
    _reject_unsupported(req)
    body: dict[str, Any] = {
        "model": model.upstream_model,
        # Anthropic requires max_tokens; OpenAI doesn't.
        "max_tokens": req.output_token_cap() or default_max_tokens,
        "messages": _messages(req.messages),
    }
    if system := system_prompt(req.messages):
        body["system"] = system
    if req.temperature is not None:
        body["temperature"] = min(req.temperature, 1.0)  # OpenAI range is 0-2, Anthropic 0-1
    elif req.top_p is not None:
        body["top_p"] = req.top_p  # newer Claude models reject temperature and top_p together
    if stops := req.stop_sequences():
        body["stop_sequences"] = stops
    if tools := function_tools(req.tools):
        body["tools"] = [_tool(f) for f in tools]
    if (choice := _tool_choice(req.tool_choice)) is not None:
        body["tool_choice"] = choice
    if stream:
        body["stream"] = True
    return body


def _reject_unsupported(req: ChatRequest) -> None:
    if req.n is not None and req.n > 1:
        raise BadRequest(NAME, "n > 1 is not supported by this model")
    fmt = (req.response_format or {}).get("type")
    if fmt in ("json_object", "json_schema"):
        raise BadRequest(NAME, f"response_format {fmt} is not supported by this model")


def _messages(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role in ("system", "developer"):
            continue
        role, blocks = _blocks(message)
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)  # e.g. several tool results in one user turn
        else:
            out.append({"role": role, "content": blocks})
    return out


def _blocks(message: ChatMessage) -> tuple[str, list[dict[str, Any]]]:
    if message.role == "tool":
        block = {
            "type": "tool_result",
            "tool_use_id": message.tool_call_id or "",
            "content": plain_text(message.content),
        }
        return "user", [block]
    blocks = [b for p in content_parts(message.content) if (b := _part(p)) is not None]
    if message.role == "assistant":
        for call in message.tool_calls or []:
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.function.name,
                    "input": parse_arguments(call.function.arguments),
                }
            )
        return "assistant", blocks
    return "user", blocks


def _part(part: dict[str, Any]) -> dict[str, Any] | None:
    kind = part.get("type")
    if kind == "text":
        text = str(part.get("text", ""))
        return {"type": "text", "text": text} if text else None  # empty text blocks are rejected
    if kind == "image_url":
        url = image_url_of(part)
        if parsed := parse_data_url(url):
            mime, data = parsed
            return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
        return {"type": "image", "source": {"type": "url", "url": url}}
    raise BadRequest(NAME, f"unsupported content part type {kind!r}")


def _tool(function: dict[str, Any]) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "name": function["name"],
        "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
    }
    if description := function.get("description"):
        tool["description"] = description
    return tool


def _tool_choice(choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, dict):
        return {"type": "tool", "name": choice.get("function", {}).get("name", "")}
    return {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": {"type": "none"}}.get(
        choice
    )


# ---- response translation ------------------------------------------------------------------


def _input_tokens(usage: dict[str, Any]) -> int:
    # Prompt caching splits input into three counters; all of them are prompt tokens.
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(int(usage.get(k) or 0) for k in keys)


def to_response(data: dict[str, Any], model: ModelSpec) -> ChatResponse:
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in data.get("content") or []:
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            arguments = json.dumps(block.get("input") or {})
            calls.append(
                ToolCall(
                    id=block["id"], function=FunctionCall(name=block["name"], arguments=arguments)
                )
            )
    text = "".join(texts)
    usage = data.get("usage") or {}
    message = ResponseMessage(content=text if text or not calls else None, tool_calls=calls or None)
    return ChatResponse(
        id=data.get("id") or new_completion_id(),
        created=now_ts(),
        model=data.get("model") or model.upstream_model,
        choices=[
            Choice(
                message=message,
                finish_reason=_FINISH_REASONS.get(data.get("stop_reason") or "", "stop"),
            )
        ],
        usage=Usage.of(_input_tokens(usage), int(usage.get("output_tokens") or 0)),
    )


@dataclass
class _StreamState:
    """Turns Anthropic stream events into OpenAI chunks."""

    id: str
    created: int
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tool_index_by_block: dict[int, int] = field(default_factory=dict)

    def handle(self, event: dict[str, Any]) -> list[ChatChunk]:
        kind = event.get("type")
        if kind == "message_start":
            return self._on_message_start(event.get("message") or {})
        if kind == "content_block_start":
            return self._on_block_start(event)
        if kind == "content_block_delta":
            return self._on_block_delta(event)
        if kind == "message_delta":
            return self._on_message_delta(event)
        if kind == "message_stop":
            usage = Usage.of(self.prompt_tokens, self.completion_tokens)
            return [ChatChunk(id=self.id, created=self.created, model=self.model, usage=usage)]
        if kind == "error":
            _raise_stream_error(event.get("error") or {})
        return []  # ping, content_block_stop

    def _chunk(self, delta: Delta, finish_reason: str | None = None) -> ChatChunk:
        choice = ChunkChoice(delta=delta, finish_reason=finish_reason)
        return ChatChunk(id=self.id, created=self.created, model=self.model, choices=[choice])

    def _on_message_start(self, message: dict[str, Any]) -> list[ChatChunk]:
        self.id = message.get("id") or self.id
        self.model = message.get("model") or self.model
        usage = message.get("usage") or {}
        self.prompt_tokens = _input_tokens(usage)
        self.completion_tokens = int(usage.get("output_tokens") or 0)
        return [self._chunk(Delta(role="assistant", content=""))]

    def _on_block_start(self, event: dict[str, Any]) -> list[ChatChunk]:
        block = event.get("content_block") or {}
        if block.get("type") != "tool_use":
            return []
        tool_index = len(self.tool_index_by_block)
        self.tool_index_by_block[int(event.get("index", 0))] = tool_index
        call = ToolCallDelta(
            index=tool_index,
            id=block.get("id"),
            type="function",
            function=FunctionCallDelta(name=block.get("name"), arguments=""),
        )
        return [self._chunk(Delta(tool_calls=[call]))]

    def _on_block_delta(self, event: dict[str, Any]) -> list[ChatChunk]:
        delta = event.get("delta") or {}
        if delta.get("type") == "text_delta":
            return [self._chunk(Delta(content=delta.get("text", "")))]
        if delta.get("type") == "input_json_delta":
            tool_index = self.tool_index_by_block.get(int(event.get("index", 0)), 0)
            fn = FunctionCallDelta(arguments=delta.get("partial_json", ""))
            return [self._chunk(Delta(tool_calls=[ToolCallDelta(index=tool_index, function=fn)]))]
        return []  # thinking / signature deltas are not part of the OpenAI format

    def _on_message_delta(self, event: dict[str, Any]) -> list[ChatChunk]:
        usage = event.get("usage") or {}
        if "output_tokens" in usage:
            self.completion_tokens = int(usage["output_tokens"] or 0)
        if usage.get("input_tokens") is not None:
            self.prompt_tokens = _input_tokens(usage)
        stop = (event.get("delta") or {}).get("stop_reason")
        if not stop:
            return []
        return [self._chunk(Delta(), finish_reason=_FINISH_REASONS.get(stop, "stop"))]


def _raise_stream_error(error: dict[str, Any]) -> None:
    kind = str(error.get("type", "error"))
    message = f"{kind}: {error.get('message', '')}"
    if kind == "rate_limit_error":
        raise RateLimited(NAME, message)
    raise ProviderUnavailable(NAME, message)  # overloaded_error, api_error, ...
