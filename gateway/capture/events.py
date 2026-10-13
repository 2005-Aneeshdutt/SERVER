"""Capture event schema: topic `gateway.events.v1` (see PLAN.md 3.1).

Versioned: consumers check `schema_version`. Message content is present only for keys that
opted in (`distill_opt_in`), and only after PII scrubbing.
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from gateway.core.schemas import ChatRequest, ChatResponse
from gateway.observability.request_log import RequestRecord

SCHEMA_VERSION = 1


class EventParams(BaseModel):
    temperature: float | None
    max_tokens: int | None
    response_format: str | None  # "json_schema" | "json_object" | None
    has_tools: bool
    stream: bool


class EventRequest(BaseModel):
    alias: str
    messages: list[dict[str, Any]] | None  # None unless the key opted in
    params: EventParams
    system_fingerprint: str | None
    template_fingerprint: str | None


class EventResponse(BaseModel):
    text: str | None  # None unless the key opted in
    finish_reason: str | None
    has_tool_calls: bool


class EventMeta(BaseModel):
    served_alias: str
    provider: str
    upstream_model: str
    status: str
    error_type: str | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: str  # Decimal as string: money is never a float
    latency_ms: int
    ttft_ms: int | None
    cache_hit: bool
    fallback_count: int
    route: str | None


class EventPii(BaseModel):
    email: int = 0
    phone: int = 0
    card: int = 0


class CaptureEvent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    event_id: str
    ts: str
    simulated: bool
    key_id: str | None
    content_captured: bool
    request: EventRequest
    response: EventResponse
    meta: EventMeta
    pii: EventPii


@dataclass(frozen=True, slots=True)
class CaptureItem:
    """What the hot path hands over: references only, no serialization or scrubbing.

    `served_alias` is the alias that actually answered; `record.alias` is what the client asked
    for (e.g. "auto").
    """

    record: RequestRecord
    served_alias: str
    body: ChatRequest
    response: ChatResponse | None
    opt_in: bool
    simulated: bool
