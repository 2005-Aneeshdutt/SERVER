from decimal import Decimal

import pytest

from gateway.core.errors import ModelNotFound
from gateway.routing.registry import ModelRegistry, ModelSpec
from tests.conftest import ROOT
from tests.helpers import spec


def test_loads_repo_models_yaml_with_exact_decimal_prices() -> None:
    registry = ModelRegistry.from_yaml(ROOT / "models.yaml")
    groq = registry.get("groq-llama-8b")
    assert groq.provider == "groq"
    assert groq.input_price_per_mtok == Decimal("0.05")
    assert isinstance(groq.input_price_per_mtok, Decimal)


def test_float_prices_are_converted_via_str_not_binary() -> None:
    model = ModelSpec(
        alias="a",
        provider="groq",
        upstream_model="x",
        input_price_per_mtok=0.1,
        output_price_per_mtok=0.3,
        context_window=10,
        tier="cheap",
    )
    assert model.input_price_per_mtok == Decimal("0.1")


def test_cost_is_per_million_tokens() -> None:
    model = spec()  # $1 in, $2 out per 1M
    assert model.cost_usd(1_000_000, 0) == Decimal(1)
    assert model.cost_usd(500, 250) == Decimal("0.001")


def test_unknown_alias_raises_model_not_found() -> None:
    with pytest.raises(ModelNotFound):
        ModelRegistry([spec()]).get("nope")


def test_fallback_to_unknown_alias_is_rejected_at_load() -> None:
    bad = spec().model_copy(update={"fallbacks": ("missing",)})
    with pytest.raises(ValueError, match="unknown fallback"):
        ModelRegistry([bad])


def test_self_fallback_is_rejected() -> None:
    bad = spec().model_copy(update={"fallbacks": ("test-model",)})
    with pytest.raises(ValueError, match="itself"):
        ModelRegistry([bad])


def test_unknown_provider_rejected() -> None:
    with pytest.raises(ValueError):
        ModelSpec(
            alias="a",
            provider="mystery",
            upstream_model="x",
            input_price_per_mtok=Decimal(0),
            output_price_per_mtok=Decimal(0),
            context_window=1,
            tier="cheap",
        )
