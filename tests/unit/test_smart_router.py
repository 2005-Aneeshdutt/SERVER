import uuid
from decimal import Decimal
from typing import Any

import pytest

from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import InvalidRequest
from gateway.routing.registry import ModelRegistry, ModelSpec
from gateway.routing.smart import RouterModel, SmartRouter, routing_text
from tests.helpers import FakeBreaker, KeywordEmbedder, request

# "hard" prompts -> P(cheap good enough) = sigmoid(-2) = 0.12; "easy" -> sigmoid(2) = 0.88
MODEL = RouterModel(
    cheap_alias="small",
    strong_alias="big",
    weights=(-2.0, 2.0),
    bias=0.0,
    thresholds={"low": 0.05, "balanced": 0.5, "high": 0.95},
)


def spec(alias: str, provider: str, **extra: Any) -> ModelSpec:
    return ModelSpec(
        alias=alias,
        provider=provider,
        upstream_model=alias,
        input_price_per_mtok=Decimal(extra.pop("price_in", "0.1")),
        output_price_per_mtok=Decimal(extra.pop("price_out", "0.1")),
        context_window=extra.pop("context_window", 8192),
        tier="cheap",
        **extra,
    )


def make_router(
    small: dict[str, Any] | None = None, big: dict[str, Any] | None = None
) -> tuple[SmartRouter, FakeBreaker, KeywordEmbedder]:
    registry = ModelRegistry(
        [
            spec("small", "groq", **(small or {})),
            spec("big", "openrouter", price_in="5", price_out="15", **(big or {})),
        ]
    )
    breaker, embedder = FakeBreaker(), KeywordEmbedder()
    return (
        SmartRouter(MODEL, embedder, registry, breaker, default_max_tokens=100),
        breaker,
        embedder,
    )


def key(quality: str = "balanced", allowed: frozenset[str] | None = None) -> ApiKeyRecord:
    return ApiKeyRecord(uuid.uuid4(), "k", None, None, None, allowed, True, quality)


EASY = request(model="auto", messages=[{"role": "user", "content": "What's the capital of Peru?"}])
HARD = request(model="auto", messages=[{"role": "user", "content": "Prove this theorem formally"}])


async def test_classifier_sends_easy_prompts_cheap_and_hard_prompts_strong() -> None:
    router, _, _ = make_router()
    easy = await router.route(EASY, key(), None)
    hard = await router.route(HARD, key(), None)
    assert (easy.route, easy.reason, easy.spec.alias) == ("cheap", "classifier", "small")
    assert (hard.route, hard.spec.alias) == ("strong", "big")
    assert easy.probability == pytest.approx(0.88, abs=0.01)
    assert easy.headers()["X-Gateway-Route-Probability"] == "0.881"


async def test_quality_setting_moves_the_threshold() -> None:
    router, _, _ = make_router()
    assert (await router.route(HARD, key("low"), None)).route == "cheap"  # 0.12 >= 0.05
    assert (await router.route(EASY, key("high"), None)).route == "strong"  # 0.88 < 0.95


async def test_required_features_override_the_classifier() -> None:
    router, _, embedder = make_router(small={"supports_tools": False})
    tools = [{"type": "function", "function": {"name": "f"}}]
    decision = await router.route(EASY.model_copy(update={"tools": tools}), key(), None)
    assert (decision.route, decision.reason) == ("strong", "rule:tools")
    assert embedder.calls == 0  # no embedding needed once a rule decides


async def test_context_length_rule() -> None:
    router, _, _ = make_router(small={"context_window": 50})
    decision = await router.route(EASY, key(), None)  # prompt + 100 output > 50
    assert (decision.route, decision.reason) == ("strong", "rule:context_length")


async def test_open_circuit_routes_to_the_other_model() -> None:
    router, breaker, _ = make_router()
    breaker.open.add("openrouter")  # the strong model's provider is down
    decision = await router.route(HARD, key(), None)
    assert (decision.route, decision.reason) == ("cheap", "rule:circuit_open")


async def test_low_remaining_budget_forces_cheap() -> None:
    router, _, _ = make_router()
    decision = await router.route(HARD, key(), remaining_budget_usd=Decimal("0.0001"))
    assert (decision.route, decision.reason) == ("cheap", "rule:budget")


async def test_key_allowlist_limits_the_candidates() -> None:
    router, _, _ = make_router()
    decision = await router.route(HARD, key(allowed=frozenset({"auto", "small"})), None)
    assert decision.spec.alias == "small"
    with pytest.raises(InvalidRequest):
        await router.route(EASY, key(allowed=frozenset({"auto"})), None)


def test_routing_text_is_the_last_user_message() -> None:
    body = request(
        messages=[
            {"role": "system", "content": "be nice"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "second"},
        ]
    )
    assert routing_text(body) == "second"


def test_router_model_round_trips_through_json(tmp_path: Any) -> None:
    path = tmp_path / "router.json"
    path.write_text(
        '{"cheap_alias": "a", "strong_alias": "b", "weights": [0.5, -0.5], "bias": 0.1,'
        ' "thresholds": {"low": 0.2, "balanced": 0.5, "high": 0.8}}',
        encoding="utf-8",
    )
    model = RouterModel.load(path)
    assert model.probability([0.0, 0.0]) == pytest.approx(0.525, abs=0.001)
