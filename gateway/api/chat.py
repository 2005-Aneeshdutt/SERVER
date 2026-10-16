"""POST /v1/chat/completions.

Pipeline: rate limit -> cache lookup (coalesced) -> spend reservation -> dispatch (retries,
fallbacks, breaker) -> respond -> release: settle spend, correct TPM, store cache, log.
Everything a request holds is tracked in `Guards` and released exactly once in `_finish`,
whatever happens (success, error, client disconnect).
"""

import contextlib
import logging
import math
import time
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Request
from fastapi.responses import Response, StreamingResponse

from gateway.api.deps import ContainerDep, KeyDep
from gateway.api.streaming import DONE_EVENT, StreamAccumulator, client_event, sse_json
from gateway.cache.response_cache import (
    CACHE_HEADER,
    CachedEntry,
    cache_key,
    is_cacheable,
    lookup_or_lead,
    replay_chunks,
)
from gateway.capture.events import CaptureItem
from gateway.container import Container
from gateway.core import tokens
from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import (
    GatewayError,
    InvalidRequest,
    ModelNotAllowed,
    ModelNotFound,
    ProviderError,
    RateLimitExceeded,
    SpendCapExceeded,
)
from gateway.core.schemas import ChatChunk, ChatRequest, ChatResponse, Usage
from gateway.distill.rollout import Decision
from gateway.limits.spend import Reservation
from gateway.observability.metrics import (
    CACHE_LOOKUPS,
    CACHE_SAVED_SECONDS,
    CACHE_SAVED_USD,
    LIMIT_REJECTIONS,
    REQUEST_LATENCY,
    REQUESTS,
    TIME_TO_FIRST_TOKEN,
    USAGE_ESTIMATED,
)
from gateway.observability.request_log import RequestRecord
from gateway.routing.dispatch import DispatchTrace
from gateway.routing.smart import AUTO, RouteDecision

log = logging.getLogger(__name__)
router = APIRouter()
SIMULATE_OUTAGE_HEADER = "x-gateway-simulate-outage"
# Marks traffic as simulated (replayer, demos) in captured events, on top of the key's flag.
SIMULATED_HEADER = "x-gateway-simulated"

_INTERNAL_ERROR_BODY = {
    "error": {
        "message": "Internal gateway error",
        "type": "api_error",
        "code": "internal_error",
        "param": None,
    }
}


@dataclass
class Guards:
    """Resources this request holds. Released exactly once, in `_finish`."""

    tpm_charged: int = 0
    reservation: Reservation | None = None
    cache_key: str | None = None
    lock_token: str | None = None  # set if we lead an in-flight cache miss
    cache_status: str = "BYPASS"  # HIT | MISS | BYPASS
    rate_headers: dict[str, str] = field(default_factory=dict)
    route: RouteDecision | None = None  # model="auto" only
    distill: Decision | None = None  # distilled-model rollout (shadow or distilled arm)
    distill_outcome: str | None = None  # served | fallback (invalid answer re-served) | shadow
    finished: bool = False


@dataclass(frozen=True)
class CallContext:
    request_id: uuid.UUID
    key: ApiKeyRecord
    requested_alias: str
    trace: DispatchTrace  # which alias/provider actually served (or last failed)
    stream: bool
    started: float
    created_at: datetime
    body: ChatRequest
    simulated: bool = False
    guards: Guards = field(default_factory=Guards)


@dataclass(frozen=True)
class Outcome:
    status: str  # ok | error | client_disconnected
    usage: Usage
    usage_estimated: bool
    error_type: str | None = None
    ttft_ms: int | None = None
    response: ChatResponse | None = None  # full response, for the cache


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    body: ChatRequest, request: Request, container: ContainerDep, key: KeyDep
) -> Response:
    if body.model == AUTO:
        if container.router is None:
            raise InvalidRequest("model 'auto' is not enabled on this gateway")
        spec = container.router.strong  # placeholder until the request is routed
    else:
        spec = container.registry.get(body.model)
    if not key.may_use(body.model):
        raise ModelNotAllowed(f"This key is not allowed to use {body.model!r}")
    call = CallContext(
        request_id=uuid.uuid4(),
        key=key,
        requested_alias=body.model,
        trace=DispatchTrace(spec=spec, simulated_down=_simulated_outage(request)),
        stream=body.stream,
        started=time.perf_counter(),
        created_at=datetime.now(UTC),
        body=body,
        simulated=key.simulated or request.headers.get(SIMULATED_HEADER, "") in ("1", "true"),
    )
    try:
        return await _handle(container, body, call, request.headers.get(CACHE_HEADER))
    except Exception as exc:
        await _finish(container, call, _failure(exc))
        raise


def _simulated_outage(request: Request) -> frozenset[str]:
    """`X-Gateway-Simulate-Outage: groq,gemini` -> providers to skip for this request only."""
    raw = request.headers.get(SIMULATE_OUTAGE_HEADER, "")
    return frozenset(p.strip().lower() for p in raw.split(",") if p.strip())


async def _handle(
    container: Container, body: ChatRequest, call: CallContext, cache_header: str | None
) -> Response:
    prompt_tokens = tokens.count_messages(body.messages)
    await _admit(container, body, call, prompt_tokens)
    if is_cacheable(body, cache_header):
        key = cache_key(call.key.id, body)  # computed on the request exactly as the client sent it
        lookup = await lookup_or_lead(container.cache, key, container.settings.cache_wait_timeout_s)
        if lookup.entry is not None:
            CACHE_LOOKUPS.labels("coalesced" if lookup.waited else "hit").inc()
            return await _serve_cached(container, body, call, lookup.entry)
        CACHE_LOOKUPS.labels("miss").inc()
        call.guards.cache_key, call.guards.lock_token = key, lookup.lock_token
        call.guards.cache_status = "MISS"
    else:
        CACHE_LOOKUPS.labels("bypass").inc()
    if call.requested_alias == AUTO:
        await _route(container, body, call)  # after the cache: a hit needs no routing
    if container.rollout is not None:
        await _distill(container, body, call)
    await _reserve(container, body, call, prompt_tokens)
    if body.stream:
        return await _stream(container, body, call)
    return await _complete(container, body, call)


async def _route(container: Container, body: ChatRequest, call: CallContext) -> None:
    assert container.router is not None
    remaining = None
    if call.key.monthly_spend_cap_usd is not None:
        remaining = await container.spend.remaining(call.key)
    decision = await container.router.route(body, call.key, remaining)
    call.trace.spec = decision.spec
    call.guards.route = decision


async def _distill(container: Container, body: ChatRequest, call: CallContext) -> None:
    """Rollout of a distilled model for a recognized task (see gateway/distill/rollout.py)."""
    assert container.rollout is not None
    decision = await container.rollout.choose(body, call.trace.spec, call.key)
    if decision is None:
        return
    call.guards.distill = decision
    if decision.arm == "distilled" and decision.spec is not None:
        call.trace.spec = decision.spec  # its fallbacks lead back to the strong model


async def _check_distilled(
    container: Container, body: ChatRequest, call: CallContext, response: ChatResponse
) -> ChatResponse:
    """Validate a distilled answer; re-serve with the strong model if it fails. Shadow mode
    compares in the background and always returns the strong answer unchanged."""
    decision, rollout = call.guards.distill, container.rollout
    if decision is None or rollout is None:
        return response
    text = response.choices[0].message.content if response.choices else None
    if decision.arm == "shadow":
        rollout.shadow(decision.task, body, text)
        call.guards.distill_outcome = "shadow"
        return response
    served_distilled = call.trace.spec.alias == decision.task.distilled_alias
    ok = served_distilled and rollout.validate(decision.task, text)
    await rollout.record(decision.task, ok)
    if ok or not served_distilled:  # not served_distilled: a fallback already used the strong model
        call.guards.distill_outcome = "served" if ok else "fallback"
        if ok:
            rollout.audit(decision.task, body, text)  # sampled, in the background
        return response
    call.guards.distill_outcome = "fallback"
    call.trace.spec = container.registry.get(decision.task.source_alias)
    return await container.dispatcher.complete(body, call.key, call.trace)


# ---- limits --------------------------------------------------------------------------------


def _output_cap(container: Container, body: ChatRequest) -> int:
    return (body.output_token_cap() or container.settings.default_max_tokens) * (body.n or 1)


async def _admit(container: Container, body: ChatRequest, call: CallContext, prompt: int) -> None:
    """RPM + TPM. TPM is charged with prompt + max output now and corrected in `_finish`."""
    estimate = prompt + _output_cap(container, body)
    decision = await container.rate_limiter.acquire(call.key, estimate)
    call.guards.rate_headers = decision.headers()
    if not decision.allowed:
        LIMIT_REJECTIONS.labels("rate").inc()
        raise RateLimitExceeded(
            "Rate limit exceeded for this API key. Retry after the time in Retry-After.",
            decision.headers(),
        )
    if call.key.tpm_limit:
        call.guards.tpm_charged = estimate


async def _reserve(container: Container, body: ChatRequest, call: CallContext, prompt: int) -> None:
    """Reserve the worst case this request could cost against the monthly cap."""
    if call.key.monthly_spend_cap_usd is None:
        return
    if body.output_token_cap() is None:
        # Cap the output so the reservation is a true upper bound (see docs/decisions/0009).
        body.max_tokens = container.settings.default_max_tokens
    prompt_bound = math.ceil(prompt * container.settings.spend_prompt_margin)
    output_bound = _output_cap(container, body)
    # A fallback may serve the request, so reserve for the most expensive candidate.
    candidates = container.dispatcher.candidates(call.trace.spec, call.key) or [call.trace.spec]
    worst = max(c.cost_usd(prompt_bound, output_bound) for c in candidates)
    try:
        call.guards.reservation = await container.spend.reserve(
            call.key, str(call.request_id), worst
        )
    except SpendCapExceeded:
        LIMIT_REJECTIONS.labels("spend").inc()
        raise


# ---- cache hits ----------------------------------------------------------------------------


async def _serve_cached(
    container: Container, body: ChatRequest, call: CallContext, entry: CachedEntry
) -> Response:
    call.guards.cache_status = "HIT"
    with contextlib.suppress(ModelNotFound):  # alias removed since caching: report requested
        call.trace.spec = container.registry.get(entry.alias)
    CACHE_SAVED_USD.inc(float(entry.cost_usd))
    CACHE_SAVED_SECONDS.inc(entry.latency_ms / 1000)
    response = entry.chat_response()
    usage = response.usage or Usage.of(0, 0)
    if body.stream:
        chunks = replay_chunks(response)
        acc = StreamAccumulator(call.started)
        for chunk in chunks:
            acc.add(chunk)
        acc.usage = usage
        return _streaming_response(container, body, call, chunks, _nothing(), acc)
    await _finish(container, call, Outcome("ok", usage, False, response=response))
    return _json(response, _headers(call, Decimal(0)))


async def _nothing() -> AsyncIterator[ChatChunk]:
    return
    yield  # pragma: no cover  (makes this an async generator)


# ---- non-streaming -------------------------------------------------------------------------


async def _complete(container: Container, body: ChatRequest, call: CallContext) -> Response:
    response = await container.dispatcher.complete(body, call.key, call.trace)
    response = await _check_distilled(container, body, call, response)
    usage, estimated = _usage_or_estimate(response.usage, body, _response_text(response))
    response.usage = usage
    cost = await _finish(container, call, Outcome("ok", usage, estimated, response=response))
    return _json(response, _headers(call, cost))


def _json(response: ChatResponse, headers: dict[str, str]) -> Response:
    # model_dump_json serializes in pydantic-core (Rust) in one pass, instead of building a
    # dict and running it through json.dumps.
    body = response.model_dump_json(exclude_none=True)
    return Response(content=body, media_type="application/json", headers=headers)


def _response_text(response: ChatResponse) -> str:
    parts: list[str] = []
    for choice in response.choices:
        parts.append(choice.message.content or "")
        for tool_call in choice.message.tool_calls or []:
            parts.extend((tool_call.function.name, tool_call.function.arguments))
    return "".join(parts)


# ---- streaming -----------------------------------------------------------------------------


async def _stream(container: Container, body: ChatRequest, call: CallContext) -> Response:
    # The dispatcher pulls chunks until the first one with content *before* we commit to a 200.
    # Anything failing before that point is retried / failed over invisibly, or becomes a real
    # HTTP error. See docs/decisions/0003 and 0005.
    primed = await container.dispatcher.stream(body, call.key, call.trace)
    acc = StreamAccumulator(call.started)
    for chunk in primed.head:
        acc.add(chunk)
    return _streaming_response(container, body, call, primed.head, primed.rest, acc)


def _streaming_response(
    container: Container,
    body: ChatRequest,
    call: CallContext,
    head: list[ChatChunk],
    rest: AsyncIterator[ChatChunk],
    acc: StreamAccumulator,
) -> StreamingResponse:
    headers = _headers(call, None) | {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return StreamingResponse(
        _sse_events(container, body, call, head, rest, acc),
        media_type="text/event-stream",
        headers=headers,
    )


async def _sse_events(
    container: Container,
    body: ChatRequest,
    call: CallContext,
    head: list[ChatChunk],
    rest: AsyncIterator[ChatChunk],
    acc: StreamAccumulator,
) -> AsyncIterator[str]:
    # Stays "client_disconnected" unless we reach the end or hit an error: if the client goes
    # away, Starlette stops iterating and this generator is closed at its current `yield`.
    status, error_type = "client_disconnected", None
    try:
        for chunk in head:
            if event := client_event(chunk):
                yield event
        async for chunk in rest:
            acc.add(chunk)
            if event := client_event(chunk):
                yield event
        if body.wants_usage_in_stream():
            usage, _ = _stream_usage(acc, body)
            final = acc.usage_chunk(usage, _fallback_id(call), call.trace.spec.upstream_model)
            payload = final.model_dump(mode="json", exclude_none=True)
            # Cost isn't known when stream headers go out; report it with the final usage.
            # Extra top-level fields are ignored by OpenAI SDKs.
            payload["x_gateway"] = {"cost_usd": format(_cost(call, usage), ".10f")}
            yield sse_json(payload)
        yield DONE_EVENT
        status = "ok"
    except Exception as exc:
        # The client has already seen content, so no failover: splicing another model's output
        # onto a half-finished answer would be worse than an honest error. Report it in-band;
        # OpenAI SDKs raise APIError on this event. See docs/decisions/0005.
        status, error_type = "error", _error_type(exc)
        if isinstance(exc, ProviderError):
            await container.breaker.record_failure(exc.provider)
        else:
            log.exception("unexpected error mid-stream")
        yield sse_json(exc.to_body() if isinstance(exc, GatewayError) else _INTERNAL_ERROR_BODY)
    finally:
        if isinstance(rest, AsyncGenerator):
            await rest.aclose()
        usage, estimated = _stream_usage(acc, body)
        response = None
        wants_response = call.guards.lock_token is not None or container.capture is not None
        if status == "ok" and wants_response:
            response = acc.to_response(usage, _fallback_id(call), call.trace.spec.upstream_model)
        outcome = Outcome(status, usage, estimated, error_type, acc.ttft_ms, response)
        await _finish(container, call, outcome)


def _cost(call: CallContext, usage: Usage) -> Decimal:
    if call.guards.cache_status == "HIT":
        return Decimal(0)
    return call.trace.spec.cost_usd(usage.prompt_tokens, usage.completion_tokens)


def _stream_usage(acc: StreamAccumulator, body: ChatRequest) -> tuple[Usage, bool]:
    return _usage_or_estimate(acc.usage, body, acc.text())


def _fallback_id(call: CallContext) -> str:
    return f"chatcmpl-{call.request_id.hex}"


# ---- shared --------------------------------------------------------------------------------


def _usage_or_estimate(
    reported: Usage | None, body: ChatRequest, completion_text: str
) -> tuple[Usage, bool]:
    if reported is not None:
        return Usage.of(reported.prompt_tokens, reported.completion_tokens), False
    prompt = tokens.count_messages(body.messages)
    return Usage.of(prompt, tokens.count_text(completion_text)), True


def _error_type(exc: BaseException) -> str:
    return type(exc).__name__ if isinstance(exc, GatewayError) else "InternalError"


def _failure(exc: BaseException) -> Outcome:
    return Outcome("error", Usage.of(0, 0), False, _error_type(exc))


def _headers(call: CallContext, cost: Decimal | None) -> dict[str, str]:
    served = call.trace.spec
    headers = {
        "X-Gateway-Request-Id": str(call.request_id),
        "X-Gateway-Provider": served.provider,
        "X-Gateway-Model": served.upstream_model,
        "X-Gateway-Alias": served.alias,
        "X-Gateway-Fallbacks": str(call.trace.fallback_count),
        "X-Gateway-Attempts": str(call.trace.attempts),
        "X-Cache": call.guards.cache_status,
        **call.guards.rate_headers,
        **(call.guards.route.headers() if call.guards.route else {}),
    }
    if call.guards.distill is not None:
        outcome = call.guards.distill_outcome or call.guards.distill.arm
        headers["X-Gateway-Distill"] = f"{call.guards.distill.task.task}; {outcome}"
    if cost is not None:
        headers["X-Gateway-Cost-USD"] = format(cost, ".10f")
    # Standard Server-Timing header: what the gateway added vs. time waiting on the provider
    # (for streams: up to the first content chunk, i.e. time-to-headers). Used by the load tests.
    elapsed_ms = (time.perf_counter() - call.started) * 1000
    upstream_ms = call.trace.upstream_s * 1000
    headers["Server-Timing"] = (
        f"gateway;dur={max(0.0, elapsed_ms - upstream_ms):.2f}, upstream;dur={upstream_ms:.2f}"
    )
    return headers


async def _finish(container: Container, call: CallContext, outcome: Outcome) -> Decimal:
    """Log the request, update metrics, release everything it held. Idempotent.

    `alias` is what the client asked for; provider/model/cost are what actually served it.
    Cache hits cost $0.
    """
    guards = call.guards
    if guards.finished:
        return Decimal(0)
    guards.finished = True
    served = call.trace.spec
    usage = outcome.usage
    cache_hit = guards.cache_status == "HIT"
    cost = (
        Decimal(0) if cache_hit else served.cost_usd(usage.prompt_tokens, usage.completion_tokens)
    )
    latency_s = time.perf_counter() - call.started
    record = RequestRecord(
        id=call.request_id,
        key_id=call.key.id,
        alias=call.requested_alias,
        provider=served.provider,
        upstream_model=served.upstream_model,
        stream=call.stream,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        usage_estimated=outcome.usage_estimated,
        cost_usd=cost,
        latency_ms=int(latency_s * 1000),
        time_to_first_token_ms=outcome.ttft_ms,
        cache_hit=cache_hit,
        status=outcome.status,
        error_type=outcome.error_type,
        fallback_count=call.trace.fallback_count,
        created_at=call.created_at,
        route=guards.route.route if guards.route else None,
        route_probability=guards.route.probability if guards.route else None,
    )
    container.request_log.submit(record)
    if container.capture is not None:  # put_nowait only: scrubbing and Kafka run off the path
        container.capture.submit(
            CaptureItem(
                record=record,
                served_alias=served.alias,
                body=call.body,
                response=outcome.response,
                opt_in=call.key.distill_opt_in,
                simulated=call.simulated,
            )
        )
    _observe(call, outcome, latency_s)
    await _release(container, call, outcome, cost, latency_s)
    return cost


def _observe(call: CallContext, outcome: Outcome, latency_s: float) -> None:
    served = call.trace.spec
    REQUESTS.labels(call.requested_alias, served.provider, outcome.status).inc()
    REQUEST_LATENCY.labels(call.requested_alias, str(call.stream).lower()).observe(latency_s)
    if outcome.ttft_ms is not None:
        TIME_TO_FIRST_TOKEN.labels(call.requested_alias).observe(outcome.ttft_ms / 1000)
    if outcome.usage_estimated:
        USAGE_ESTIMATED.labels(served.provider).inc()
        log.info(
            "usage estimated with tokenizer: provider=%s request=%s",
            served.provider,
            call.request_id,
        )


async def _release(
    container: Container, call: CallContext, outcome: Outcome, cost: Decimal, latency_s: float
) -> None:
    """Settle spend, true-up TPM, publish to the cache. Each step swallows its own errors."""
    guards = call.guards
    if guards.reservation is not None:
        await container.spend.settle(guards.reservation, cost)
    if guards.tpm_charged:
        used = 0 if guards.cache_status == "HIT" else outcome.usage.total_tokens
        await container.rate_limiter.adjust_tokens(call.key, guards.tpm_charged - used)
    if guards.cache_key is not None and guards.lock_token is not None:
        if outcome.status == "ok" and outcome.response is not None:
            served = call.trace.spec
            entry = CachedEntry(
                response=outcome.response.model_dump(mode="json", exclude_none=True),
                alias=served.alias,
                provider=served.provider,
                upstream_model=served.upstream_model,
                cost_usd=str(cost),
                latency_ms=int(latency_s * 1000),
            )
            await container.cache.set(guards.cache_key, entry)
        await container.cache.unlock(guards.cache_key, guards.lock_token)
