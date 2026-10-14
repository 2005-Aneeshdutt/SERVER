"""Rollout of distilled models (M10): shadow -> canary -> full, with automatic rollback.

State lives in Redis so every replica agrees:
    distill:tasks                set of task fingerprints
    distill:task:{fp}            hash: task, source_alias, distilled_alias, validator (JSON),
                                 version, state (off|shadow|canary|full|rolled_back), weight,
                                 shadow_n, shadow_valid, shadow_agree
    distill:window:{fp}          per-minute online outcome counters (rollout_record.lua)

Hot path cost when enabled: a system-prompt fingerprint (a hash) and a dict lookup. Task state
is cached per replica and refreshed at most every `refresh_s`, so there's no Redis read per
request (ADR 0006's lesson). Outcomes are written only for requests a distilled model served.

Arms:
- shadow: the strong model serves; afterwards, in the background (bounded concurrency), the
  distilled model answers the same request and is compared. Never affects the response.
- canary/full: with probability `weight`, the distilled model serves. Its answer is validated;
  an invalid answer is re-served by the strong model in the same request and counts as a
  failure. Failures feed the atomic rollback check in Lua.
Streaming requests always go to the strong model (a stream can't be validated before sending).
"""

import asyncio
import json
import logging
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.capture.fingerprint import system_fingerprint
from gateway.core.auth import ApiKeyRecord
from gateway.core.schemas import ChatRequest
from gateway.distill.validators import normalize_label, validate
from gateway.observability.metrics import DISTILL_REQUESTS, REDIS_ERRORS
from gateway.providers.base import Provider
from gateway.routing.registry import ModelRegistry, ModelSpec

log = logging.getLogger(__name__)
STATES = ("off", "shadow", "canary", "full", "rolled_back")
_LUA = Path(__file__).parent / "lua" / "rollout_record.lua"

AuditSink = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True)
class TaskRollout:
    fingerprint: str
    task: str
    source_alias: str
    distilled_alias: str
    validator: dict[str, Any]
    version: str
    state: str
    weight: float

    @classmethod
    def from_hash(cls, fp: str, raw: Mapping[bytes | str, bytes | str]) -> "TaskRollout":
        h = {_text(k): _text(v) for k, v in raw.items()}
        return cls(
            fingerprint=fp,
            task=h["task"],
            source_alias=h["source_alias"],
            distilled_alias=h["distilled_alias"],
            validator=json.loads(h["validator"]),
            version=h.get("version", "v1"),
            state=h.get("state", "off"),
            weight=float(h.get("weight", "0")),
        )


@dataclass(frozen=True)
class Decision:
    task: TaskRollout
    arm: str  # "distilled" | "shadow"
    spec: ModelSpec | None = None  # the distilled model, for arm == "distilled"


@dataclass(frozen=True)
class RolloutConfig:
    refresh_s: float = 2.0
    window_minutes: int = 10
    min_samples: int = 50
    failure_threshold: float = 0.05
    shadow_concurrency: int = 4


def task_key(fp: str) -> str:
    return f"distill:task:{{{fp}}}"


def window_key(fp: str) -> str:
    return f"distill:window:{{{fp}}}"


class RolloutController:
    def __init__(
        self,
        redis: Redis,
        registry: ModelRegistry,
        providers: Mapping[str, Provider],
        audit: AuditSink,
        config: RolloutConfig | None = None,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._redis = redis
        self._registry = registry
        self._providers = providers
        self._audit = audit
        self._config = config or RolloutConfig()
        self._rng = rng
        self._record = redis.register_script(_LUA.read_text())
        self._tasks: dict[str, TaskRollout] = {}
        self._loaded_at = -1e9
        self._shadow_slots = asyncio.Semaphore(self._config.shadow_concurrency)
        self._background: set[asyncio.Task[None]] = set()

    # ---- state ------------------------------------------------------------------------------

    async def tasks(self, force: bool = False) -> dict[str, TaskRollout]:
        now = time.monotonic()
        if force or now - self._loaded_at >= self._config.refresh_s:
            self._loaded_at = now
            try:
                fps = [_text(m) for m in await self._redis.smembers("distill:tasks")]
                loaded = {}
                for fp in fps:
                    raw = await self._redis.hgetall(task_key(fp))
                    if raw:
                        loaded[fp] = TaskRollout.from_hash(fp, raw)
                self._tasks = loaded
            except RedisError:
                REDIS_ERRORS.labels(component="distill_rollout").inc()  # keep the stale view
        return self._tasks

    async def register(self, fields: dict[str, Any], actor: str) -> None:
        fp = fields["system_fingerprint"]
        await self._redis.sadd("distill:tasks", fp)
        await self._redis.hset(
            task_key(fp),
            mapping={
                "task": fields["task"],
                "source_alias": fields["source_alias"],
                "distilled_alias": fields["distilled_alias"],
                "validator": json.dumps(fields["validator"]),
                "version": fields.get("version", "v1"),
                "state": "off",
                "weight": "0",
            },
        )
        await self._audit_event(fields["task"], fp, "register", "off", 0.0, actor, "registered")
        await self.tasks(force=True)

    async def set_state(self, fp: str, state: str, weight: float, actor: str, reason: str) -> None:
        if state not in STATES:
            raise ValueError(f"unknown state {state!r}")
        weight = weight if state in ("canary", "full") else 0.0
        await self._redis.hset(task_key(fp), mapping={"state": state, "weight": str(weight)})
        task = (await self.tasks(force=True))[fp]
        await self._audit_event(task.task, fp, state, state, weight, actor, reason)

    async def stats(self, fp: str) -> dict[str, Any]:
        """Shadow comparison counts and the current online window, for the dashboard."""
        raw = await self._redis.hgetall(task_key(fp))
        h = {_text(k): _text(v) for k, v in raw.items()}
        window = {
            _text(k): int(_text(v)) for k, v in (await self._redis.hgetall(window_key(fp))).items()
        }
        minute = int(time.time() // 60)
        recent = range(minute - self._config.window_minutes + 1, minute + 1)
        return {
            "shadow_n": int(h.get("shadow_n", 0)),
            "shadow_valid": int(h.get("shadow_valid", 0)),
            "shadow_agree": int(h.get("shadow_agree", 0)),
            "window_n": sum(window.get(f"{m}:n", 0) for m in recent),
            "window_failures": sum(window.get(f"{m}:f", 0) for m in recent),
        }

    # ---- hot path ---------------------------------------------------------------------------

    async def choose(
        self, body: ChatRequest, spec: ModelSpec, key: ApiKeyRecord
    ) -> Decision | None:
        if body.stream or not key.distill_serving:
            return None
        tasks = await self.tasks()
        if not tasks:
            return None
        fp = system_fingerprint(body.messages)
        task = tasks.get(fp) if fp else None
        if task is None or spec.alias != task.source_alias:
            return None
        if task.state == "shadow":
            return Decision(task, "shadow")
        if task.state in ("canary", "full") and self._rng() < task.weight:
            DISTILL_REQUESTS.labels(task.task, "distilled").inc()
            return Decision(task, "distilled", self._registry.get(task.distilled_alias))
        return None

    async def record(self, task: TaskRollout, ok: bool) -> bool:
        """Count a distilled outcome; True if this outcome triggered an automatic rollback."""
        if not ok:
            DISTILL_REQUESTS.labels(task.task, "failed").inc()
        c = self._config
        try:
            rolled, n, failures = await self._record(
                keys=[task_key(task.fingerprint), window_key(task.fingerprint)],
                args=[int(not ok), c.window_minutes, c.min_samples, c.failure_threshold, 1.96],
            )
        except RedisError:
            REDIS_ERRORS.labels(component="distill_rollout").inc()
            return False
        if rolled:
            DISTILL_REQUESTS.labels(task.task, "rolled_back").inc()
            reason = f"auto: {failures}/{n} failures in {c.window_minutes} min"
            self._spawn(
                self._audit_event(
                    task.task, task.fingerprint, "rollback", "rolled_back", 0.0, "auto", reason
                )
            )
            await self.tasks(force=True)
        return bool(rolled)

    def validate(self, task: TaskRollout, text: str | None) -> bool:
        return validate(task.validator, text)

    def shadow(self, task: TaskRollout, body: ChatRequest, strong_text: str | None) -> None:
        """Compare the distilled model with the strong answer, off the request path."""
        if self._shadow_slots.locked():
            DISTILL_REQUESTS.labels(task.task, "shadow_skipped").inc()
            return
        self._spawn(self._shadow(task, body, strong_text))

    async def _shadow(self, task: TaskRollout, body: ChatRequest, strong_text: str | None) -> None:
        async with self._shadow_slots:
            spec = self._registry.get(task.distilled_alias)
            provider = self._providers.get(spec.provider)
            text: str | None = None
            if provider is not None:
                try:
                    response = await provider.complete(body, spec)
                    text = response.choices[0].message.content if response.choices else None
                except Exception:
                    log.info("shadow call failed for %s", task.task)
            valid = self.validate(task, text)
            agree = valid and _same(text, strong_text)
            DISTILL_REQUESTS.labels(task.task, "shadow").inc()
            try:
                key = task_key(task.fingerprint)
                pipe = self._redis.pipeline(transaction=False)
                pipe.hincrby(key, "shadow_n", 1)
                pipe.hincrby(key, "shadow_valid", int(valid))
                pipe.hincrby(key, "shadow_agree", int(agree))
                await pipe.execute()
            except RedisError:
                REDIS_ERRORS.labels(component="distill_rollout").inc()

    # ---- plumbing ---------------------------------------------------------------------------

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _audit_event(
        self, task: str, fp: str, action: str, state: str, weight: float, actor: str, reason: str
    ) -> None:
        try:
            await self._audit(
                {
                    "task": task,
                    "system_fingerprint": fp,
                    "action": action,
                    "state": state,
                    "weight": weight,
                    "actor": actor,
                    "reason": reason,
                }
            )
        except Exception:
            log.exception("could not write distill audit event")

    async def close(self) -> None:
        if self._background:
            await asyncio.wait(set(self._background), timeout=5)


def _text(value: bytes | str) -> str:
    return value.decode() if isinstance(value, bytes) else value


def _same(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and normalize_label(a) == normalize_label(b)
