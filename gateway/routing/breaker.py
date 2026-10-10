"""Per-provider circuit breaker with state in Redis, shared by every gateway replica.

closed --(N failures within window)--> open --(cooldown elapses)--> half-open
half-open: exactly one request (the probe) is let through; success closes, failure re-opens.
All transitions are single Lua scripts, so concurrent replicas can't race each other.
If Redis is unreachable the breaker fails OPEN (allows traffic): it's an optimization, and
the retry/fallback logic still protects requests. See docs/decisions/0006.
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.observability.metrics import BREAKER_STATE, REDIS_ERRORS

log = logging.getLogger(__name__)
_LUA = Path(__file__).parent / "lua"

CLOSED, OPEN, HALF_OPEN = 0, 1, 2  # gauge values
CLOSED_ALLOWED = 1  # breaker_allow.lua: 1 closed, 2 half-open probe, 0 rejected


class Breaker(Protocol):
    async def allow(self, provider: str) -> bool: ...
    async def record_success(self, provider: str) -> None: ...
    async def record_failure(self, provider: str) -> bool:
        """Returns True if the circuit is open after this failure."""
        ...

    async def is_open(self, provider: str) -> bool:
        """Read-only check (never takes the half-open probe). Used for routing decisions."""
        ...


@dataclass(frozen=True)
class BreakerConfig:
    failure_threshold: int = 5
    window_s: float = 30.0
    cooldown_s: float = 30.0
    probe_ttl_s: float = 15.0
    # How long an open circuit stays "half-open" waiting for a probe before resetting to closed.
    idle_reset_s: float = 600.0
    # Remember "closed" locally this long, skipping the Redis check on most requests. Another
    # replica opening the circuit is noticed within this delay. 0 disables.
    closed_cache_s: float = 1.0


class RedisCircuitBreaker:
    def __init__(
        self,
        redis: Redis,
        config: BreakerConfig,
        prefix: str = "cb",
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._redis = redis
        self._clock = clock
        self._closed_until: dict[str, float] = {}
        self._prefix = prefix
        self._allow = redis.register_script((_LUA / "breaker_allow.lua").read_text())
        self._failure = redis.register_script((_LUA / "breaker_failure.lua").read_text())
        self._success = redis.register_script((_LUA / "breaker_success.lua").read_text())
        # Half-open probes this replica holds. A success only changes state when it's a probe's
        # (closed circuits ignore successes), so other successes skip the Redis round trip.
        self._probes: dict[str, int] = {}

    def _keys(self, provider: str) -> list[str]:
        base = f"{self._prefix}:{{{provider}}}"  # hash tag keeps all keys on one cluster slot
        return [f"{base}:failures", f"{base}:open_until", f"{base}:probe"]

    async def allow(self, provider: str) -> bool:
        if self._closed_until.get(provider, 0.0) > self._clock():
            return True  # recently seen closed: skip the round trip (see closed_cache_s)
        try:
            result = int(
                await self._allow(keys=self._keys(provider), args=[_ms(self._config.probe_ttl_s)])
            )
        except RedisError:
            _redis_down("allow")
            return True
        if result == CLOSED_ALLOWED:
            self._closed_until[provider] = self._clock() + self._config.closed_cache_s
        if result == HALF_OPEN:
            self._probes[provider] = self._probes.get(provider, 0) + 1
            BREAKER_STATE.labels(provider).set(HALF_OPEN)
        return result != 0

    async def is_open(self, provider: str) -> bool:
        if self._closed_until.get(provider, 0.0) > self._clock():
            return False
        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                pipe.time()
                pipe.get(self._keys(provider)[1])
                (seconds, micros), open_until = await pipe.execute()
        except RedisError:
            _redis_down("is_open")
            return False
        if open_until is None:
            return False
        return int(seconds) * 1000 + int(micros) // 1000 < int(open_until)

    def _end_probe(self, provider: str) -> bool:
        held = self._probes.get(provider, 0)
        if held:
            self._probes[provider] = held - 1
        return held > 0

    async def record_success(self, provider: str) -> None:
        if not self._end_probe(provider):
            return  # closed circuit: nothing to change, no Redis call
        try:
            closed = int(await self._success(keys=self._keys(provider)))
        except RedisError:
            _redis_down("record_success")
            return
        if closed:
            log.info("circuit closed: provider=%s", provider)
            BREAKER_STATE.labels(provider).set(CLOSED)

    async def record_failure(self, provider: str) -> bool:
        self._end_probe(provider)
        c = self._config
        args: list[int | str] = [
            c.failure_threshold,
            _ms(c.window_s),
            _ms(c.cooldown_s),
            uuid.uuid4().hex,
            _ms(c.cooldown_s + c.idle_reset_s),
        ]
        try:
            is_open = bool(int(await self._failure(keys=self._keys(provider), args=args)))
        except RedisError:
            _redis_down("record_failure")
            return False
        if is_open:
            self._closed_until.pop(provider, None)
            BREAKER_STATE.labels(provider).set(OPEN)
        return is_open


def _ms(seconds: float) -> int:
    return int(seconds * 1000)


def _redis_down(operation: str) -> None:
    REDIS_ERRORS.labels(component="breaker").inc()
    log.warning("redis unavailable for circuit breaker %s; failing open", operation)
