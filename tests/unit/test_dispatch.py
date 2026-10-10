from collections.abc import AsyncIterator
from decimal import Decimal

import pytest

from gateway.core.errors import (
    AuthError,
    BadRequest,
    NoProviderAvailable,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)
from gateway.core.schemas import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    Choice,
    ChunkChoice,
    Delta,
    ResponseMessage,
)
from gateway.providers.base import Provider
from gateway.routing.dispatch import Dispatcher, DispatchTrace, RetryPolicy
from gateway.routing.registry import ModelRegistry, ModelSpec
from tests.helpers import FakeBreaker, key_record, request

Script = list[ProviderError | str]  # per call: raise the error, or answer with the text


class ScriptedProvider:
    """Answers each call from a script; records how many calls it got."""

    def __init__(self, name: str, script: Script) -> None:
        self.name = name
        self.script = script
        self.calls = 0

    def _next(self) -> str:
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, ProviderError):
            raise step
        return step

    async def complete(self, req: ChatRequest, model: ModelSpec) -> ChatResponse:
        text = self._next()
        message = ResponseMessage(content=text)
        return ChatResponse(
            id="x", created=0, model=model.upstream_model, choices=[Choice(message=message)]
        )

    async def stream(self, req: ChatRequest, model: ModelSpec) -> AsyncIterator[ChatChunk]:
        yield _chunk(Delta(role="assistant", content=""))
        yield _chunk(Delta(content=self._next()))


def _chunk(delta: Delta) -> ChatChunk:
    return ChatChunk(id="x", created=0, model="m", choices=[ChunkChoice(delta=delta)])


def _spec(alias: str, provider: str, fallbacks: tuple[str, ...] = ()) -> ModelSpec:
    return ModelSpec(
        alias=alias,
        provider=provider,
        upstream_model=f"{provider}-model",
        input_price_per_mtok=Decimal(0),
        output_price_per_mtok=Decimal(0),
        context_window=1000,
        tier="cheap",
        fallbacks=fallbacks,
    )


REGISTRY = ModelRegistry(
    [_spec("main", "groq", ("backup", "last")), _spec("backup", "gemini"), _spec("last", "openai")]
)


class Harness:
    def __init__(self, scripts: dict[str, Script], total_timeout_s: float = 60) -> None:
        self.providers = {name: ScriptedProvider(name, s) for name, s in scripts.items()}
        self.breaker = FakeBreaker()
        self.sleeps: list[float] = []
        self.now = 0.0

        async def sleep(seconds: float) -> None:
            self.sleeps.append(seconds)
            self.now += seconds

        providers: dict[str, Provider] = dict(self.providers)
        self.dispatcher = Dispatcher(
            REGISTRY,
            providers,
            self.breaker,
            policy=RetryPolicy(max_attempts_per_provider=2, base_delay_s=0.2, max_delay_s=2.0),
            first_token_timeout_s=5,
            total_timeout_s=total_timeout_s,
            sleep=sleep,
            rng=lambda: 1.0,  # deterministic "jitter": always the max of the window
            clock=lambda: self.now,
        )

    async def complete(
        self, allowed: frozenset[str] | None = None
    ) -> tuple[str | None, DispatchTrace]:
        trace = DispatchTrace(spec=REGISTRY.get("main"))
        response = await self.dispatcher.complete(request(), key_record(allowed), trace)
        return response.choices[0].message.content, trace


def unavailable(p: str = "groq") -> ProviderUnavailable:
    return ProviderUnavailable(p, "503")


async def test_first_try_success() -> None:
    h = Harness({"groq": ["ok"], "gemini": ["backup"]})
    text, trace = await h.complete()
    assert (text, trace.attempts, trace.fallback_count) == ("ok", 1, 0)
    assert h.breaker.successes == ["groq"] and h.sleeps == []


async def test_retries_same_provider_with_exponential_backoff() -> None:
    h = Harness({"groq": [unavailable(), "ok"]})
    text, trace = await h.complete()
    assert (text, trace.attempts, trace.fallback_count) == ("ok", 2, 0)
    assert h.sleeps == [0.2]  # base * 2**0, full jitter with rng=1.0
    assert h.breaker.failures == ["groq"]


async def test_fails_over_after_max_attempts() -> None:
    h = Harness({"groq": [unavailable()], "gemini": ["backup"]})
    text, trace = await h.complete()
    assert (text, trace.spec.alias, trace.fallback_count, trace.attempts) == (
        "backup",
        "backup",
        1,
        3,
    )
    assert h.providers["groq"].calls == 2


async def test_bad_request_is_never_retried_or_failed_over() -> None:
    h = Harness({"groq": [BadRequest("groq", "bad")], "gemini": ["backup"]})
    with pytest.raises(BadRequest):
        await h.complete()
    assert h.providers["groq"].calls == 1 and h.providers["gemini"].calls == 0
    assert h.breaker.failures == []


async def test_auth_error_fails_over_without_retry() -> None:
    h = Harness({"groq": [AuthError("groq", "bad key")], "gemini": ["backup"]})
    text, _ = await h.complete()
    assert text == "backup"
    assert h.providers["groq"].calls == 1 and h.sleeps == []


async def test_short_retry_after_is_honoured() -> None:
    h = Harness({"groq": [RateLimited("groq", "slow", retry_after_s=1.5), "ok"]})
    text, _ = await h.complete()
    assert text == "ok" and h.sleeps == [1.5]


async def test_long_retry_after_moves_to_next_provider_immediately() -> None:
    h = Harness({"groq": [RateLimited("groq", "slow", retry_after_s=60)], "gemini": ["backup"]})
    text, _ = await h.complete()
    assert text == "backup"
    assert h.providers["groq"].calls == 1 and h.sleeps == []


async def test_open_circuit_is_skipped() -> None:
    h = Harness({"groq": ["primary"], "gemini": ["backup"]})
    h.breaker.open.add("groq")
    text, trace = await h.complete()
    assert text == "backup" and trace.skipped == ["main"]
    assert h.providers["groq"].calls == 0


async def test_all_circuits_open_is_503() -> None:
    h = Harness({"groq": ["a"], "gemini": ["b"], "openai": ["c"]})
    h.breaker.open.update({"groq", "gemini", "openai"})
    with pytest.raises(NoProviderAvailable) as info:
        await h.complete()
    assert info.value.status_code == 503


async def test_failure_that_opens_circuit_stops_retrying_that_provider() -> None:
    h = Harness({"groq": [unavailable()], "gemini": ["backup"]})
    h.breaker.open.add("groq")  # record_failure reports "open" ...
    h.dispatcher.providers = dict(h.providers)
    h.breaker.open.discard("groq")  # ... but allow() let the first call in (e.g. half-open probe)

    async def failure_opens(provider: str) -> bool:
        h.breaker.failures.append(provider)
        return True

    h.breaker.record_failure = failure_opens  # type: ignore[method-assign]
    text, _ = await h.complete()
    assert text == "backup" and h.providers["groq"].calls == 1


async def test_all_fail_raises_last_error() -> None:
    h = Harness(
        {
            "groq": [unavailable("groq")],
            "gemini": [unavailable("gemini")],
            "openai": [ProviderTimeout("openai", "slow")],
        }
    )
    with pytest.raises(ProviderTimeout):
        await h.complete()
    assert [h.providers[p].calls for p in ("groq", "gemini", "openai")] == [2, 2, 2]


async def test_fallbacks_respect_key_allowlist_and_configured_providers() -> None:
    h = Harness({"groq": [unavailable()], "openai": ["last"]})  # p2 not configured
    text, trace = await h.complete(allowed=frozenset({"main", "last"}))
    assert text == "last" and trace.fallback_count == 1  # index among usable candidates

    h = Harness({"groq": [unavailable()], "gemini": ["backup"], "openai": ["last"]})
    with pytest.raises(ProviderUnavailable):
        await h.complete(allowed=frozenset({"main"}))


async def test_backoff_that_would_overshoot_deadline_skips_retry() -> None:
    h = Harness({"groq": [unavailable()], "gemini": ["backup"]}, total_timeout_s=0.1)
    text, _ = await h.complete()  # 0.2s backoff > 0.1s budget: don't retry, fail over instead
    assert text == "backup"
    assert h.providers["groq"].calls == 1 and h.sleeps == []


async def test_exhausted_deadline_stops_everything() -> None:
    h = Harness({"groq": [unavailable()], "gemini": ["backup"]}, total_timeout_s=5)
    groq = h.providers["groq"]
    original = groq.complete

    async def slow_failure(req: ChatRequest, model: ModelSpec) -> ChatResponse:
        h.now += 10  # this attempt used up the whole budget
        return await original(req, model)

    groq.complete = slow_failure  # type: ignore[method-assign]
    with pytest.raises(ProviderTimeout, match="deadline"):
        await h.complete()
    assert h.providers["gemini"].calls == 0


async def test_stream_failover_before_first_content() -> None:
    h = Harness({"groq": [unavailable()], "gemini": ["hello"]})
    trace = DispatchTrace(spec=REGISTRY.get("main"))
    primed = await h.dispatcher.stream(request(stream=True), key_record(), trace)
    rest = [c async for c in primed.rest]
    texts = [c.choices[0].delta.content for c in primed.head + rest]
    assert texts == ["", "hello"]  # role chunk + first content chunk, both from p2
    assert trace.spec.alias == "backup"
