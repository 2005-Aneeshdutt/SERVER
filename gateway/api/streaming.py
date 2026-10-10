"""SSE framing and per-stream bookkeeping (TTFT, emitted text, usage, reassembled response)."""

import json
import time
from dataclasses import dataclass, field
from typing import Any

from gateway.core.schemas import (
    ChatChunk,
    ChatResponse,
    Choice,
    FunctionCall,
    ResponseMessage,
    ToolCall,
    Usage,
)

DONE_EVENT = "data: [DONE]\n\n"


def sse_json(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"


def client_event(chunk: ChatChunk) -> str | None:
    """SSE line to forward, or None for usage-only chunks (the gateway emits its own)."""
    if not chunk.choices:
        return None
    return f"data: {chunk.model_dump_json(exclude_none=True, exclude={'usage'})}\n\n"


@dataclass
class _ToolCallState:
    id: str = ""
    name: str = ""
    arguments: list[str] = field(default_factory=list)


@dataclass
class _ChoiceState:
    content: list[str] = field(default_factory=list)
    tool_calls: dict[int, _ToolCallState] = field(default_factory=dict)
    finish_reason: str | None = None


class StreamAccumulator:
    def __init__(self, started: float) -> None:
        self._started = started
        self._choices: dict[int, _ChoiceState] = {}
        self.ttft_ms: int | None = None
        self.usage: Usage | None = None
        self.id: str | None = None
        self.model: str | None = None
        self.created: int | None = None

    def add(self, chunk: ChatChunk) -> None:
        self.id = self.id or chunk.id
        self.model = self.model or chunk.model
        self.created = self.created or chunk.created
        if chunk.usage is not None:
            self.usage = chunk.usage
        if self.ttft_ms is None and chunk.has_content():
            self.ttft_ms = int((time.perf_counter() - self._started) * 1000)
        for choice in chunk.choices:
            state = self._choices.setdefault(choice.index, _ChoiceState())
            if choice.delta.content:
                state.content.append(choice.delta.content)
            for delta in choice.delta.tool_calls or []:
                call = state.tool_calls.setdefault(delta.index, _ToolCallState())
                call.id = delta.id or call.id
                if delta.function is not None:
                    call.name = delta.function.name or call.name
                    if delta.function.arguments:
                        call.arguments.append(delta.function.arguments)
            if choice.finish_reason:
                state.finish_reason = choice.finish_reason

    def text(self) -> str:
        """Everything the model produced (for token estimation)."""
        parts: list[str] = []
        for state in self._choices.values():
            parts.extend(state.content)
            for call in state.tool_calls.values():
                parts.append(call.name)
                parts.extend(call.arguments)
        return "".join(parts)

    def to_response(self, usage: Usage, fallback_id: str, fallback_model: str) -> ChatResponse:
        """Reassemble the equivalent non-streamed response (used to cache streamed answers)."""
        choices = []
        for index in sorted(self._choices):
            state = self._choices[index]
            calls = [
                ToolCall(
                    id=c.id, function=FunctionCall(name=c.name, arguments="".join(c.arguments))
                )
                for _, c in sorted(state.tool_calls.items())
            ]
            content = "".join(state.content)
            message = ResponseMessage(
                content=content if content or not calls else None, tool_calls=calls or None
            )
            choices.append(Choice(index=index, message=message, finish_reason=state.finish_reason))
        return ChatResponse(
            id=self.id or fallback_id,
            created=self.created or int(time.time()),
            model=self.model or fallback_model,
            choices=choices,
            usage=usage,
        )

    def usage_chunk(self, usage: Usage, fallback_id: str, fallback_model: str) -> ChatChunk:
        return ChatChunk(
            id=self.id or fallback_id,
            created=self.created or int(time.time()),
            model=self.model or fallback_model,
            choices=[],
            usage=usage,
        )
