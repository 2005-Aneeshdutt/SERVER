"""Retries, fallback chains and circuit breaking around provider calls.

For each candidate (requested alias, then its `fallbacks` in order):
  skip it if its circuit is open; otherwise try up to N times, retrying only RateLimited /
  ProviderUnavailable / ProviderTimeout with exponential backoff + full jitter.
BadRequest is the client's fault: it is raised immediately, never retried or failed over.
AuthError is our credential problem with *that* provider: no retry, but fail over.
Everything runs inside one overall deadline (`total_timeout_s`). See docs/decisions/0005.
"""

import asyncio
import random
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import (
    BadRequest,
    NoProviderAvailable,
    ProviderError,
    ProviderTimeout,
    RateLimited,
)
from gateway.core.schemas import ChatChunk, ChatRequest, ChatResponse
from gateway.observability.metrics import FALLBACKS, UPSTREAM_ATTEMPTS
from gateway.providers.base import Provider
from gateway.providers.timeouts import with_stream_timeouts, with_total_timeout
from gateway.routing.breaker import Breaker
from gateway.routing.registry import ModelRegistry, ModelSpec


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts_per_provider: int = 2
    base_delay_s: float = 0.2
    max_delay_s: float = 2.0

    def delay(self, attempt: int, error: ProviderError, rng: Callable[[], float]) -> float | None:
        """Seconds to wait before retrying the same provider, or None to move to the next one."""
        if not error.retryable:
            return None
        if isinstance(error, RateLimited) and error.retry_after_s is not None:
            # Honour the provider's hint if it's short; otherwise another provider is faster.
            return error.retry_after_s if error.retry_after_s <= self.max_delay_s else None
        return rng() * min(self.max_delay_s, self.base_delay_s * 2.0**attempt)  # full jitter


@dataclass
class DispatchTrace:
    """What happened while serving one request. Filled in as we go, so it's useful on failure."""

    spec: ModelSpec  # candidate currently/last tried; the served one on success
    attempts: int = 0
    fallback_count: int = 0
    skipped: list[str] = field(default_factory=list)  # aliases skipped (circuit open)
    # Time spent waiting on providers (streams: until the first content chunk), so
    # total - upstream_s = what the gateway itself added.
    upstream_s: float = 0.0
    # Providers to treat as down for THIS request only (X-Gateway-Simulate-Outage). Lets the
    # public playground demonstrate failover without touching shared circuit-breaker state.
    simulated_down: frozenset[str] = frozenset()


@dataclass
class PrimedStream:
    """A stream that has already produced its first content chunk (or ended without one)."""

    head: list[ChatChunk]
    rest: AsyncGenerator[ChatChunk, None]


class Dispatcher:
    def __init__(
        self,
        registry: ModelRegistry,
        providers: Mapping[str, Provider],
        breaker: Breaker,
        *,
        policy: RetryPolicy,
        first_token_timeout_s: float,
        total_timeout_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.providers = providers
        self._registry = registry
        self._breaker = breaker
        self._policy = policy
        self._first_token_s = first_token_timeout_s
        self._total_s = total_timeout_s
        self._sleep = sleep
        self._rng = rng
        self._clock = clock

    def candidates(self, spec: ModelSpec, key: ApiKeyRecord) -> list[ModelSpec]:
        chain = [spec, *(self._registry.get(alias) for alias in spec.fallbacks)]
        return [s for s in chain if s.provider in self.providers and key.may_use(s.alias)]

    async def complete(
        self, req: ChatRequest, key: ApiKeyRecord, trace: DispatchTrace
    ) -> ChatResponse:
        async def call(provider: Provider, spec: ModelSpec, budget_s: float) -> ChatResponse:
            return await with_total_timeout(provider.complete(req, spec), provider.name, budget_s)

        return await self._run(key, trace, call)

    async def stream(
        self, req: ChatRequest, key: ApiKeyRecord, trace: DispatchTrace
    ) -> PrimedStream:
        async def call(provider: Provider, spec: ModelSpec, budget_s: float) -> PrimedStream:
            chunks = with_stream_timeouts(
                provider.stream(req, spec), provider.name, self._first_token_s, budget_s
            )
            return await _prime(chunks)

        return await self._run(key, trace, call)

    async def _run[T](
        self,
        key: ApiKeyRecord,
        trace: DispatchTrace,
        call: Callable[[Provider, ModelSpec, float], Awaitable[T]],
    ) -> T:
        requested = trace.spec
        candidates = self.candidates(requested, key)
        if not candidates:
            raise NoProviderAvailable(f"No configured provider can serve {requested.alias!r}")
        deadline = self._clock() + self._total_s
        last_error: ProviderError | None = None
        for index, spec in enumerate(candidates):
            provider = self.providers[spec.provider]
            if provider.name in trace.simulated_down:
                UPSTREAM_ATTEMPTS.labels(provider.name, "simulated_outage").inc()
                trace.skipped.append(spec.alias)
                continue
            if not await self._breaker.allow(provider.name):
                UPSTREAM_ATTEMPTS.labels(provider.name, "circuit_open").inc()
                trace.skipped.append(spec.alias)
                continue
            trace.spec, trace.fallback_count = spec, index
            try:
                result = await self._attempts(provider, spec, deadline, trace, call)
            except ProviderError as exc:
                if isinstance(exc, BadRequest):
                    raise
                last_error = exc
                continue
            if spec.alias != requested.alias:
                FALLBACKS.labels(requested.alias, spec.alias).inc()
            return result
        if last_error is not None:
            raise last_error
        raise NoProviderAvailable(
            f"All providers for {requested.alias!r} are unavailable (circuit open): "
            + ", ".join(trace.skipped)
        )

    async def _attempts[T](
        self,
        provider: Provider,
        spec: ModelSpec,
        deadline: float,
        trace: DispatchTrace,
        call: Callable[[Provider, ModelSpec, float], Awaitable[T]],
    ) -> T:
        """Try one provider up to N times. Raises the last ProviderError if all attempts fail."""
        attempt = 0
        while True:
            budget = deadline - self._clock()
            if budget <= 0:
                raise ProviderTimeout(provider.name, "overall request deadline exceeded")
            trace.attempts += 1
            started = time.perf_counter()
            try:
                result = await call(provider, spec, budget)
            except ProviderError as exc:
                trace.upstream_s += time.perf_counter() - started
                UPSTREAM_ATTEMPTS.labels(provider.name, type(exc).__name__).inc()
                if isinstance(exc, BadRequest):
                    raise
                circuit_open = await self._breaker.record_failure(provider.name)
                attempt += 1
                delay = self._policy.delay(attempt - 1, exc, self._rng)
                if (
                    circuit_open
                    or delay is None
                    or attempt >= self._policy.max_attempts_per_provider
                    or self._clock() + delay >= deadline
                ):
                    raise
                await self._sleep(delay)
                continue
            trace.upstream_s += time.perf_counter() - started
            UPSTREAM_ATTEMPTS.labels(provider.name, "ok").inc()
            await self._breaker.record_success(provider.name)
            return result


async def _prime(chunks: AsyncGenerator[ChatChunk, None]) -> PrimedStream:
    """Pull chunks until the first one with content. Errors before that point are still
    invisible to the client, so the caller can retry or fail over."""
    head: list[ChatChunk] = []
    try:
        async for chunk in chunks:
            head.append(chunk)
            if chunk.has_content():
                break
    except BaseException:
        await chunks.aclose()
        raise
    return PrimedStream(head, chunks)
