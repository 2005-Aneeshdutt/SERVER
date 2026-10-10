"""Google Gemini (native `generateContent` API) adapter."""

import json
from collections import defaultdict
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx

from gateway.core.errors import BadRequest, ProviderUnavailable
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
from gateway.providers.base import new_completion_id, new_tool_call_id, now_ts
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

NAME = "gemini"

_FILTERED = frozenset(
    {"SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII", "IMAGE_SAFETY"}
)
_TOOL_MODES = {"auto": "AUTO", "none": "NONE", "required": "ANY"}


class GeminiProvider:
    name = NAME

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        self._client = client
        self._base = base_url.rstrip("/")
        self._headers = {"x-goog-api-key": api_key}

    async def complete(self, req: ChatRequest, model: ModelSpec) -> ChatResponse:
        url = f"{self._base}/models/{model.upstream_model}:generateContent"
        data = await post_json(self._client, NAME, url, self._headers, build_payload(req))
        return to_response(data, model)

    async def stream(self, req: ChatRequest, model: ModelSpec) -> AsyncIterator[ChatChunk]:
        url = f"{self._base}/models/{model.upstream_model}:streamGenerateContent?alt=sse"
        state = _StreamState(id=new_completion_id(), created=now_ts(), model=model.upstream_model)
        async with open_stream(self._client, NAME, url, self._headers, build_payload(req)) as resp:
            async for _event, data in iter_sse(resp):
                for chunk in state.handle(parse_json_object(NAME, data)):
                    yield chunk
        if state.usage is not None:
            yield ChatChunk(
                id=state.id, created=state.created, model=state.model, usage=state.usage
            )


# ---- request translation -------------------------------------------------------------------


def build_payload(req: ChatRequest) -> dict[str, Any]:
    body: dict[str, Any] = {"contents": _contents(req.messages)}
    if system := system_prompt(req.messages):
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if config := _generation_config(req):
        body["generationConfig"] = config
    if tools := function_tools(req.tools):
        body["tools"] = [{"functionDeclarations": [_declaration(f) for f in tools]}]
    if (tool_config := _tool_config(req.tool_choice)) is not None:
        body["toolConfig"] = tool_config
    return body


def _contents(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    # Gemini's functionResponse needs the function *name*; OpenAI tool messages only carry the
    # call id, so remember names from earlier assistant tool calls.
    names_by_call_id: dict[str, str] = {}
    out: list[dict[str, Any]] = []
    for message in messages:
        if message.role in ("system", "developer"):
            continue
        role, parts = _parts_for(message, names_by_call_id)
        if out and out[-1]["role"] == role:
            out[-1]["parts"].extend(parts)
        else:
            out.append({"role": role, "parts": parts})
    return out


def _parts_for(
    message: ChatMessage, names_by_call_id: dict[str, str]
) -> tuple[str, list[dict[str, Any]]]:
    if message.role == "tool":
        name = names_by_call_id.get(message.tool_call_id or "") or message.name or "tool"
        response = _tool_response(plain_text(message.content))
        return "user", [{"functionResponse": {"name": name, "response": response}}]
    parts = [_part(p) for p in content_parts(message.content)]
    if message.role != "assistant":
        return "user", parts
    for call in message.tool_calls or []:
        names_by_call_id[call.id] = call.function.name
        args = parse_arguments(call.function.arguments)
        parts.append({"functionCall": {"name": call.function.name, "args": args}})
    return "model", parts


def _part(part: dict[str, Any]) -> dict[str, Any]:
    kind = part.get("type")
    if kind == "text":
        return {"text": str(part.get("text", ""))}
    if kind == "image_url":
        parsed = parse_data_url(image_url_of(part))
        if parsed is None:
            raise BadRequest(NAME, "only base64 data: URLs are supported for images")
        mime, data = parsed
        return {"inlineData": {"mimeType": mime, "data": data}}
    raise BadRequest(NAME, f"unsupported content part type {kind!r}")


def _tool_response(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except ValueError:
        return {"result": text}
    return value if isinstance(value, dict) else {"result": value}


def _generation_config(req: ChatRequest) -> dict[str, Any]:
    config: dict[str, Any] = {}
    if req.temperature is not None:
        config["temperature"] = req.temperature
    if req.top_p is not None:
        config["topP"] = req.top_p
    if (cap := req.output_token_cap()) is not None:
        config["maxOutputTokens"] = cap
    if stops := req.stop_sequences():
        config["stopSequences"] = stops
    if req.n is not None:
        config["candidateCount"] = req.n
    if req.seed is not None:
        config["seed"] = req.seed
    fmt = req.response_format or {}
    if fmt.get("type") in ("json_object", "json_schema"):
        config["responseMimeType"] = "application/json"
        if schema := (fmt.get("json_schema") or {}).get("schema"):
            config["responseJsonSchema"] = schema
    return config


def _declaration(function: dict[str, Any]) -> dict[str, Any]:
    declaration: dict[str, Any] = {"name": function["name"]}
    if description := function.get("description"):
        declaration["description"] = description
    if parameters := function.get("parameters"):
        # parametersJsonSchema takes standard JSON Schema (the older `parameters` field only
        # accepts Google's OpenAPI subset).
        declaration["parametersJsonSchema"] = parameters
    return declaration


def _tool_config(choice: str | dict[str, Any] | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    if isinstance(choice, dict):
        name = choice.get("function", {}).get("name", "")
        return {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": [name]}}
    mode = _TOOL_MODES.get(choice)
    return {"functionCallingConfig": {"mode": mode}} if mode else None


# ---- response translation ------------------------------------------------------------------


def to_usage(meta: dict[str, Any] | None) -> Usage | None:
    if not meta:
        return None
    prompt = int(meta.get("promptTokenCount") or 0) + int(meta.get("toolUsePromptTokenCount") or 0)
    # Thinking tokens are billed as output tokens.
    completion = int(meta.get("candidatesTokenCount") or 0) + int(
        meta.get("thoughtsTokenCount") or 0
    )
    return Usage.of(prompt, completion)


def _finish_reason(reason: str | None, has_tool_calls: bool) -> str | None:
    if reason is None:
        return None
    if reason == "MAX_TOKENS":
        return "length"
    if reason in _FILTERED:
        return "content_filter"
    return "tool_calls" if has_tool_calls else "stop"


def _candidate_output(candidate: dict[str, Any]) -> tuple[str, list[ToolCall]]:
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(p["text"] for p in parts if "text" in p and not p.get("thought"))
    calls = [
        ToolCall(
            id=fc.get("id") or new_tool_call_id(),
            function=FunctionCall(name=fc["name"], arguments=json.dumps(fc.get("args") or {})),
        )
        for p in parts
        if (fc := p.get("functionCall"))
    ]
    return text, calls


def to_response(data: dict[str, Any], model: ModelSpec) -> ChatResponse:
    choices: list[Choice] = []
    for candidate in data.get("candidates") or []:
        text, calls = _candidate_output(candidate)
        message = ResponseMessage(
            content=text if text or not calls else None, tool_calls=calls or None
        )
        finish = _finish_reason(candidate.get("finishReason"), bool(calls))
        choices.append(
            Choice(index=int(candidate.get("index", 0)), message=message, finish_reason=finish)
        )
    if not choices:  # prompt itself was blocked (promptFeedback.blockReason)
        choices = [Choice(message=ResponseMessage(content=""), finish_reason="content_filter")]
    return ChatResponse(
        id=data.get("responseId") or new_completion_id(),
        created=now_ts(),
        model=data.get("modelVersion") or model.upstream_model,
        choices=choices,
        usage=to_usage(data.get("usageMetadata")),
    )


@dataclass
class _StreamState:
    """Gemini streams whole GenerateContentResponse objects; each carries a slice of output."""

    id: str
    created: int
    model: str
    usage: Usage | None = None
    started: bool = False
    tool_counts: defaultdict[int, int] = field(default_factory=lambda: defaultdict(int))

    def handle(self, event: dict[str, Any]) -> list[ChatChunk]:
        if "error" in event:
            raise ProviderUnavailable(NAME, f"error mid-stream: {json.dumps(event['error'])}")
        self.model = event.get("modelVersion") or self.model
        self.usage = to_usage(event.get("usageMetadata")) or self.usage  # cumulative; keep latest
        chunks: list[ChatChunk] = []
        if not self.started:
            self.started = True
            chunks.append(self._chunk(0, Delta(role="assistant", content="")))
        candidates = event.get("candidates") or []
        for candidate in candidates:
            chunks.extend(self._on_candidate(candidate))
        if not candidates and (event.get("promptFeedback") or {}).get("blockReason"):
            chunks.append(self._chunk(0, Delta(), "content_filter"))
        return chunks

    def _on_candidate(self, candidate: dict[str, Any]) -> list[ChatChunk]:
        index = int(candidate.get("index", 0))
        text, calls = _candidate_output(candidate)
        start = self.tool_counts[index]
        deltas = [
            ToolCallDelta(
                index=start + i,
                id=call.id,
                type="function",
                function=FunctionCallDelta(
                    name=call.function.name, arguments=call.function.arguments
                ),
            )
            for i, call in enumerate(calls)
        ]
        self.tool_counts[index] += len(calls)
        chunks: list[ChatChunk] = []
        if text or deltas:
            chunks.append(
                self._chunk(index, Delta(content=text or None, tool_calls=deltas or None))
            )
        if reason := candidate.get("finishReason"):
            has_calls = self.tool_counts[index] > 0
            chunks.append(self._chunk(index, Delta(), _finish_reason(reason, has_calls)))
        return chunks

    def _chunk(self, index: int, delta: Delta, finish_reason: str | None = None) -> ChatChunk:
        choice = ChunkChoice(index=index, delta=delta, finish_reason=finish_reason)
        return ChatChunk(id=self.id, created=self.created, model=self.model, choices=[choice])
