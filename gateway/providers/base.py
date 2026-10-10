"""Provider adapter interface. Input and output are always OpenAI-shaped."""

import time
import uuid
from collections.abc import AsyncIterator
from typing import Protocol

from gateway.core.schemas import ChatChunk, ChatRequest, ChatResponse
from gateway.routing.registry import ModelSpec


class Provider(Protocol):
    name: str

    async def complete(self, req: ChatRequest, model: ModelSpec) -> ChatResponse: ...

    def stream(self, req: ChatRequest, model: ModelSpec) -> AsyncIterator[ChatChunk]:
        """Yield OpenAI chunks. The final chunk carries `usage` if the provider reports it."""
        ...


def new_completion_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex}"


def new_tool_call_id() -> str:
    return f"call_{uuid.uuid4().hex[:24]}"


def now_ts() -> int:
    return int(time.time())
