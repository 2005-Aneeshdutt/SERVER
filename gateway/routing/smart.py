"""`model: "auto"`: pick the cheap or the strong model per request.

1. Hard rules first (cheap to evaluate, never wrong in a way that breaks the request):
   required features (tools / vision / JSON mode), context length, provider circuit state,
   remaining monthly budget.
2. Otherwise a classifier: P(cheap answer is as good as strong) from the prompt embedding,
   compared with the key's quality threshold. No LLM call on the hot path.
See docs/decisions/0012 and eval/ for how the classifier was trained and evaluated.
"""

import asyncio
import json
import math
import time
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Literal

from gateway.core import tokens
from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import InvalidRequest
from gateway.core.schemas import ChatRequest
from gateway.observability.metrics import ROUTER_DECISIONS, ROUTER_LATENCY
from gateway.routing.breaker import Breaker
from gateway.routing.embedder import Embedder
from gateway.routing.registry import ModelRegistry, ModelSpec

AUTO = "auto"
Quality = Literal["low", "balanced", "high"]
QUALITIES: tuple[Quality, ...] = ("low", "balanced", "high")


@dataclass(frozen=True)
class RouterModel:
    """Logistic regression over prompt embeddings, exported from eval/ as plain JSON
    (no pickle: loading it can't execute code)."""

    cheap_alias: str
    strong_alias: str
    weights: tuple[float, ...]
    bias: float
    thresholds: dict[str, float]  # quality setting -> min P(cheap is good enough)

    @classmethod
    def load(cls, path: Path) -> "RouterModel":
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            cheap_alias=raw["cheap_alias"],
            strong_alias=raw["strong_alias"],
            weights=tuple(float(w) for w in raw["weights"]),
            bias=float(raw["bias"]),
            thresholds={k: float(v) for k, v in raw["thresholds"].items()},
        )

    def probability(self, embedding: Sequence[float]) -> float:
        z = self.bias + sum(w * x for w, x in zip(self.weights, embedding, strict=True))
        return 1.0 / (1.0 + math.exp(-z))


@dataclass(frozen=True)
class RouteDecision:
    spec: ModelSpec
    route: Literal["cheap", "strong"]
    reason: str  # "classifier" or "rule:<name>"
    probability: float | None
    latency_ms: float

    def headers(self) -> dict[str, str]:
        headers = {
            "X-Gateway-Route": self.route,
            "X-Gateway-Route-Reason": self.reason,
            "X-Gateway-Route-Ms": f"{self.latency_ms:.1f}",
        }
        if self.probability is not None:
            headers["X-Gateway-Route-Probability"] = f"{self.probability:.3f}"
        return headers


def routing_text(body: ChatRequest) -> str:
    """The last user message: what the classifier was trained on (single-turn prompts)."""
    for message in reversed(body.messages):
        if message.role == "user":
            return tokens.message_text(message)
    return tokens.message_text(body.messages[-1])


def _has_images(body: ChatRequest) -> bool:
    return any(
        isinstance(m.content, list) and any(p.get("type") == "image_url" for p in m.content)
        for m in body.messages
    )


def _wants_json(body: ChatRequest) -> bool:
    return (body.response_format or {}).get("type") in ("json_object", "json_schema")


class SmartRouter:
    def __init__(
        self,
        model: RouterModel,
        embedder: Embedder,
        registry: ModelRegistry,
        breaker: Breaker,
        default_max_tokens: int,
    ) -> None:
        self._model = model
        self._embedder = embedder
        self._cheap = registry.get(model.cheap_alias)
        self._strong = registry.get(model.strong_alias)
        self._breaker = breaker
        self._default_max_tokens = default_max_tokens
        # One worker: embeddings are CPU-bound; torch's own threads parallelize each one.
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="router-embed")

    @property
    def strong(self) -> ModelSpec:
        return self._strong

    async def warm_up(self) -> None:
        await asyncio.get_running_loop().run_in_executor(self._pool, self._embedder.embed, ["hi"])

    def close(self) -> None:
        self._pool.shutdown(wait=False)

    async def route(
        self, body: ChatRequest, key: ApiKeyRecord, remaining_budget_usd: Decimal | None
    ) -> RouteDecision:
        started = time.perf_counter()
        decision = await self._decide(body, key, remaining_budget_usd)
        latency_ms = (time.perf_counter() - started) * 1000
        ROUTER_DECISIONS.labels(decision[1], decision[2]).inc()
        ROUTER_LATENCY.observe(latency_ms / 1000)
        spec, route, reason, probability = decision
        return RouteDecision(spec, route, reason, probability, latency_ms)

    async def _decide(
        self, body: ChatRequest, key: ApiKeyRecord, remaining_budget_usd: Decimal | None
    ) -> tuple[ModelSpec, Literal["cheap", "strong"], str, float | None]:
        candidates = [s for s in (self._cheap, self._strong) if key.may_use(s.alias)]
        if not candidates:
            raise InvalidRequest("This key may not use any of the models behind 'auto'.")
        candidates, rule = self._apply_feature_rules(body, candidates)
        if len(candidates) == 1:
            return self._pick(candidates[0], rule or "rule:allowed_models")
        if (circuit := await self._breaker_rule()) is not None:
            return circuit
        if (budget := self._budget_rule(body, remaining_budget_usd)) is not None:
            return budget
        embedding = (
            await asyncio.get_running_loop().run_in_executor(
                self._pool, self._embedder.embed, [routing_text(body)]
            )
        )[0]
        probability = self._model.probability(embedding)
        threshold = self._model.thresholds[key.routing_quality]
        spec = self._cheap if probability >= threshold else self._strong
        return self._pick(spec, "classifier", probability)

    def _pick(
        self, spec: ModelSpec, reason: str, probability: float | None = None
    ) -> tuple[ModelSpec, Literal["cheap", "strong"], str, float | None]:
        route: Literal["cheap", "strong"] = "cheap" if spec.alias == self._cheap.alias else "strong"
        return spec, route, reason, probability

    def _apply_feature_rules(
        self, body: ChatRequest, candidates: list[ModelSpec]
    ) -> tuple[list[ModelSpec], str | None]:
        """Drop models that can't serve the request at all."""
        checks: list[tuple[str, bool, str]] = [
            ("tools", bool(body.tools), "supports_tools"),
            ("vision", _has_images(body), "supports_vision"),
            ("json", _wants_json(body), "supports_json"),
        ]
        rule: str | None = None
        for name, needed, attr in checks:
            if needed:
                kept = [s for s in candidates if getattr(s, attr)]
                if len(kept) < len(candidates):
                    rule = f"rule:{name}"
                candidates = kept
        prompt = tokens.count_messages(body.messages)
        output = body.output_token_cap() or self._default_max_tokens
        fitting = [s for s in candidates if prompt + output <= s.context_window]
        if len(fitting) < len(candidates):
            rule = "rule:context_length"
        if not fitting:
            raise InvalidRequest("No model behind 'auto' supports this request.")
        return fitting, rule

    async def _breaker_rule(
        self,
    ) -> tuple[ModelSpec, Literal["cheap", "strong"], str, float | None] | None:
        cheap_down = await self._breaker.is_open(self._cheap.provider)
        strong_down = await self._breaker.is_open(self._strong.provider)
        if cheap_down and not strong_down:
            return self._pick(self._strong, "rule:circuit_open")
        if strong_down and not cheap_down:
            return self._pick(self._cheap, "rule:circuit_open")
        return None

    def _budget_rule(
        self, body: ChatRequest, remaining_usd: Decimal | None
    ) -> tuple[ModelSpec, Literal["cheap", "strong"], str, float | None] | None:
        """If the strong model's worst case doesn't fit the remaining budget, go cheap."""
        if remaining_usd is None:
            return None
        prompt = tokens.count_messages(body.messages)
        output = body.output_token_cap() or self._default_max_tokens
        if self._strong.cost_usd(prompt, output) > remaining_usd:
            return self._pick(self._cheap, "rule:budget")
        return None
