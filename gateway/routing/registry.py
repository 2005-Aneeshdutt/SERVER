"""Model registry: aliases from `models.yaml` mapped to provider, upstream id and prices."""

from collections.abc import Iterable
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from gateway.core.errors import ModelNotFound

# "replay" serves recorded answers for simulated traffic only (distill/replay_llm.py).
KNOWN_PROVIDERS = frozenset(
    {"openai", "anthropic", "gemini", "groq", "openrouter", "ollama", "replay", "llamacpp"}
)
_PER_MILLION = Decimal(1_000_000)


class ModelSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    alias: str
    provider: str
    upstream_model: str
    input_price_per_mtok: Decimal = Field(ge=0)
    output_price_per_mtok: Decimal = Field(ge=0)
    context_window: int = Field(gt=0)
    tier: Literal["cheap", "strong"]
    fallbacks: tuple[str, ...] = ()
    supports_tools: bool = True
    supports_vision: bool = False
    supports_json: bool = True
    # Distilled models (M8): the task they were fine-tuned for. Only reachable through a
    # rollout (gateway/distill/rollout.py), which substitutes them for the task's source alias.
    distilled_task: str | None = None

    @field_validator("input_price_per_mtok", "output_price_per_mtok", mode="before")
    @classmethod
    def _no_binary_floats(cls, value: Any) -> Any:
        # YAML parses 0.05 as a float; go through str so Decimal gets the exact literal.
        return str(value) if isinstance(value, float) else value

    @field_validator("provider")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        if value not in KNOWN_PROVIDERS:
            raise ValueError(f"unknown provider {value!r}")
        return value

    def cost_usd(self, prompt_tokens: int, completion_tokens: int) -> Decimal:
        return (
            Decimal(prompt_tokens) * self.input_price_per_mtok
            + Decimal(completion_tokens) * self.output_price_per_mtok
        ) / _PER_MILLION


class ModelRegistry:
    def __init__(self, specs: Iterable[ModelSpec]) -> None:
        self._specs = {spec.alias: spec for spec in specs}
        self._validate_fallbacks()

    @classmethod
    def from_yaml(cls, path: str | Path) -> "ModelRegistry":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        models: dict[str, dict[str, Any]] = raw["models"]
        return cls(ModelSpec(alias=alias, **fields) for alias, fields in models.items())

    def _validate_fallbacks(self) -> None:
        for spec in self._specs.values():
            for target in spec.fallbacks:
                if target == spec.alias:
                    raise ValueError(f"{spec.alias}: fallback chain references itself")
                if target not in self._specs:
                    raise ValueError(f"{spec.alias}: unknown fallback alias {target!r}")

    def get(self, alias: str) -> ModelSpec:
        try:
            return self._specs[alias]
        except KeyError:
            raise ModelNotFound(f"The model {alias!r} does not exist") from None

    def all(self) -> list[ModelSpec]:
        return list(self._specs.values())
