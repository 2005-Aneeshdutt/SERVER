"""Circuit breaker against a real Redis (docker compose up -d redis)."""

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from gateway.core.config import Settings
from gateway.routing.breaker import BreakerConfig, RedisCircuitBreaker

pytestmark = pytest.mark.integration

FAST = BreakerConfig(
    failure_threshold=3, window_s=1.0, cooldown_s=0.3, probe_ttl_s=5.0, closed_cache_s=0
)


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(Settings().redis_url)
    yield client
    await client.aclose()


@pytest.fixture
def provider() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"  # isolate tests from each other


async def test_opens_after_threshold_within_window(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    assert await breaker.record_failure(provider) is False
    assert await breaker.record_failure(provider) is False
    assert await breaker.allow(provider)
    assert await breaker.record_failure(provider) is True
    assert not await breaker.allow(provider)


async def test_failures_outside_window_do_not_count(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    await breaker.record_failure(provider)
    await breaker.record_failure(provider)
    await asyncio.sleep(3.0)  # window 1s + margin for Docker Desktop clock steps (~1s)
    assert await breaker.record_failure(provider) is False
    assert await breaker.allow(provider)


async def test_state_is_shared_between_replicas(redis: Redis, provider: str) -> None:
    other_redis = Redis.from_url(Settings().redis_url)  # separate connection = another replica
    try:
        replica_a = RedisCircuitBreaker(redis, FAST)
        replica_b = RedisCircuitBreaker(other_redis, FAST)
        await replica_a.record_failure(provider)
        await replica_b.record_failure(provider)
        await replica_a.record_failure(provider)
        assert not await replica_b.allow(provider)
    finally:
        await other_redis.aclose()


async def open_circuit(breaker: RedisCircuitBreaker, provider: str) -> None:
    for _ in range(FAST.failure_threshold):
        await breaker.record_failure(provider)
    assert not await breaker.allow(provider)


async def test_exactly_one_probe_after_cooldown_under_concurrency(
    redis: Redis, provider: str
) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    await open_circuit(breaker, provider)
    await asyncio.sleep(3.0)  # cooldown 0.3s + margin for Docker Desktop clock steps (~1s)

    # 50 requests arrive at once across "replicas": only one may probe the provider.
    clients = [Redis.from_url(Settings().redis_url) for _ in range(5)]
    try:
        breakers = [RedisCircuitBreaker(c, FAST) for c in clients]
        results = await asyncio.gather(*(breakers[i % 5].allow(provider) for i in range(50)))
    finally:
        await asyncio.gather(*(c.aclose() for c in clients))
    assert sum(results) == 1


async def test_probe_success_closes_circuit(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    await open_circuit(breaker, provider)
    await asyncio.sleep(3.0)  # cooldown 0.3s + margin for Docker Desktop clock steps (~1s)
    assert await breaker.allow(provider)  # the probe
    await breaker.record_success(provider)
    assert all([await breaker.allow(provider) for _ in range(5)])  # closed: everyone allowed


async def test_probe_failure_reopens_for_another_cooldown(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    await open_circuit(breaker, provider)
    await asyncio.sleep(3.0)  # cooldown 0.3s + margin for Docker Desktop clock steps (~1s)
    assert await breaker.allow(provider)
    assert await breaker.record_failure(provider) is True
    assert not await breaker.allow(provider)
    await asyncio.sleep(3.0)  # cooldown 0.3s + margin for Docker Desktop clock steps (~1s)
    assert await breaker.allow(provider)  # next probe after the new cooldown


async def test_success_while_open_does_not_close_early(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    await open_circuit(breaker, provider)
    await breaker.record_success(provider)  # straggler request that started before opening
    assert not await breaker.allow(provider)


async def test_closed_state_is_cached_locally_for_a_short_time(redis: Redis, provider: str) -> None:
    cached = BreakerConfig(failure_threshold=3, window_s=1.0, cooldown_s=0.3, closed_cache_s=60)
    replica_a = RedisCircuitBreaker(redis, cached)
    other = Redis.from_url(Settings().redis_url)
    try:
        replica_b = RedisCircuitBreaker(other, FAST)
        assert await replica_a.allow(provider)  # sees "closed", caches it
        for _ in range(3):
            await replica_b.record_failure(provider)  # another replica opens the circuit
        assert not await replica_b.allow(provider)
        assert await replica_a.allow(provider)  # stale for up to closed_cache_s: by design
        assert await replica_a.record_failure(provider) is True  # its own failure sees "open"
        assert not await replica_a.allow(provider)  # ...and drops the cached "closed"
    finally:
        await other.aclose()


async def test_operator_kill_switch_overrides_a_closed_circuit(redis: Redis, provider: str) -> None:
    breaker = RedisCircuitBreaker(redis, FAST)
    assert await breaker.state(provider) == "closed"
    await breaker.set_forced_down(provider, True)
    assert await breaker.state(provider) == "forced_down"
    assert not await breaker.allow(provider)
    assert await breaker.is_open(provider)
    await breaker.set_forced_down(provider, False)
    assert await breaker.state(provider) == "closed"
    assert await breaker.allow(provider)
