"""Distilled-model rollout on real Redis: arm selection, online validation with fallback to the
strong model, shadow mode, and an atomic auto-rollback shared by several replicas."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import httpx
import pytest
import respx
from redis.asyncio import Redis

from gateway.core.auth import hash_key
from gateway.core.config import Settings
from gateway.distill.rollout import RolloutConfig, RolloutController, task_key, window_key
from gateway.providers.llamacpp import LlamaCppProvider
from tests.conftest import API_KEY, Gateway
from tests.helpers import GROQ_URL, openai_completion

pytestmark = pytest.mark.integration
LLAMA_URL = "http://llamacpp:8080/v1/chat/completions"
SYSTEM = "You are the intent classifier for a bank. Reply with one label."
LABELS = ["card_arrival", "lost_or_stolen_card"]


class Audit:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def write(self, event: dict[str, Any]) -> None:
        self.events.append(event)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(Settings().redis_url)
    yield client
    # Shared Redis: never leave test tasks behind (they'd show up in a running gateway).
    from gateway.capture.fingerprint import system_fingerprint
    from gateway.core.schemas import ChatMessage

    fp = system_fingerprint([ChatMessage(role="system", content=SYSTEM)])
    assert fp is not None
    await client.srem("distill:tasks", fp)
    await client.delete(task_key(fp), window_key(fp))
    await client.aclose()


def body(text: str = "where is my card?", stream: bool = False) -> dict[str, Any]:
    return {
        "model": "groq-gpt-oss-120b",
        "stream": stream,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}],
    }


async def setup(
    gateway: Gateway, http_client: httpx.AsyncClient, redis: Redis, **config: Any
) -> tuple[RolloutController, str, Audit]:
    """Gateway with a rollout controller, a llama.cpp provider and a registered task."""
    from gateway.capture.fingerprint import system_fingerprint
    from gateway.core.schemas import ChatMessage

    # Same mocked transport as the other providers (respx routes in the `upstream` fixture).
    providers = gateway.container.providers
    assert isinstance(providers, dict)
    providers["llamacpp"] = LlamaCppProvider(http_client, "http://llamacpp:8080/v1")
    gateway.keys.records[hash_key(API_KEY)] = replace(gateway.key, distill_serving=True)
    audit = Audit()
    rollout = RolloutController(
        redis,
        gateway.container.registry,
        gateway.container.providers,
        audit.write,
        RolloutConfig(refresh_s=0, **config),
    )
    gateway.container.rollout = rollout
    fp = system_fingerprint([ChatMessage(role="system", content=SYSTEM)])
    assert fp is not None
    # A fresh fingerprint namespace per test: these keys are shared Redis state.
    await redis.delete(task_key(fp), window_key(fp))
    await rollout.register(
        {
            "task": f"bank-intent-{uuid.uuid4().hex[:6]}",
            "system_fingerprint": fp,
            "source_alias": "groq-gpt-oss-120b",
            "distilled_alias": "distilled-banking77-intent-v1",
            "validator": {"type": "label_set", "labels": LABELS},
        },
        actor="test",
    )
    return rollout, fp, audit


async def test_off_and_shadow_never_serve_the_distilled_model(
    gateway: Gateway, upstream: respx.Router, redis: Redis, http_client: httpx.AsyncClient
) -> None:
    rollout, fp, _ = await setup(gateway, http_client, redis)
    groq = upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("card_arrival"))
    )
    llama = upstream.post(LLAMA_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("card_arrival"))
    )
    r = await gateway.client.post("/v1/chat/completions", json=body())
    assert r.headers["x-gateway-alias"] == "groq-gpt-oss-120b" and not llama.called

    await rollout.set_state(fp, "shadow", 0, actor="test", reason="start shadow")
    r = await gateway.client.post("/v1/chat/completions", json=body("my card is late"))
    assert r.json()["choices"][0]["message"]["content"] == "card_arrival"
    assert r.headers["x-gateway-alias"] == "groq-gpt-oss-120b"
    assert r.headers["x-gateway-distill"].endswith("shadow")
    await rollout.close()  # wait for the background shadow call
    stats = await redis.hgetall(task_key(fp))
    assert (stats[b"shadow_n"], stats[b"shadow_valid"], stats[b"shadow_agree"]) == (
        b"1",
        b"1",
        b"1",
    )
    assert groq.call_count == 2 and llama.call_count == 1


async def test_full_rollout_serves_distilled_and_falls_back_on_invalid_answers(
    gateway: Gateway, upstream: respx.Router, redis: Redis, http_client: httpx.AsyncClient
) -> None:
    rollout, fp, _ = await setup(gateway, http_client, redis)
    await rollout.set_state(fp, "full", 1.0, actor="test", reason="go")
    groq = upstream.post(GROQ_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("lost_or_stolen_card"))
    )
    llama = upstream.post(LLAMA_URL).mock(
        side_effect=[
            httpx.Response(200, json=openai_completion("card_arrival")),
            httpx.Response(200, json=openai_completion("not a label")),
        ]
    )

    ok = await gateway.client.post("/v1/chat/completions", json=body("a"))
    assert ok.headers["x-gateway-alias"] == "distilled-banking77-intent-v1"
    assert ok.headers["x-gateway-distill"].endswith("served")
    assert ok.headers["x-gateway-cost-usd"] == "0.0000000000"
    sent = json.loads(llama.calls[0].request.content)
    assert sent["messages"][0]["content"].startswith("task: ")  # rewritten to the trained format
    assert not groq.called

    bad = await gateway.client.post("/v1/chat/completions", json=body("b"))
    assert bad.json()["choices"][0]["message"]["content"] == "lost_or_stolen_card"
    assert bad.headers["x-gateway-alias"] == "groq-gpt-oss-120b"
    assert bad.headers["x-gateway-distill"].endswith("fallback")
    assert groq.call_count == 1

    streamed = await gateway.client.post("/v1/chat/completions", json=body("c", stream=True))
    assert "x-gateway-distill" not in streamed.headers  # streams always use the strong model


async def test_rollback_is_atomic_across_replicas(
    gateway: Gateway, upstream: respx.Router, redis: Redis, http_client: httpx.AsyncClient
) -> None:
    rollout, fp, audit = await setup(
        gateway, http_client, redis, min_samples=20, failure_threshold=0.05
    )
    await rollout.set_state(fp, "canary", 0.25, actor="test", reason="canary")
    task = (await rollout.tasks(force=True))[fp]
    replicas = [
        RolloutController(
            redis,
            gateway.container.registry,
            {},
            audit.write,
            RolloutConfig(refresh_s=0, min_samples=20),
        )
        for _ in range(3)
    ]
    # 60 failures arrive concurrently on 3 replicas.
    results = await asyncio.gather(*(replicas[i % 3].record(task, ok=False) for i in range(60)))
    for r in replicas:
        await r.close()

    assert sum(results) == 1  # exactly one replica performed the rollback
    state = await redis.hget(task_key(fp), "state")
    assert state == b"rolled_back"
    assert [e["action"] for e in audit.events].count("rollback") == 1
    assert (await rollout.tasks(force=True))[fp].weight == 0.0


async def test_no_rollback_below_min_samples_or_when_healthy(
    gateway: Gateway, upstream: respx.Router, redis: Redis, http_client: httpx.AsyncClient
) -> None:
    rollout, fp, _ = await setup(
        gateway, http_client, redis, min_samples=50, failure_threshold=0.05
    )
    await rollout.set_state(fp, "full", 1.0, actor="test", reason="go")
    task = (await rollout.tasks(force=True))[fp]
    # Too few samples: 49 failures in a row still don't trigger (min_samples is 50).
    assert not any([await rollout.record(task, ok=False) for _ in range(49)])
    await redis.delete(window_key(fp))
    # A true ~5% failure rate (1 in 20, spread out) over 200 requests: the Wilson lower bound
    # stays below the 5% threshold, so a healthy model isn't rolled back by noise.
    assert not any([await rollout.record(task, ok=i % 20 != 0) for i in range(200)])
    assert (await redis.hget(task_key(fp), "state")) == b"full"
