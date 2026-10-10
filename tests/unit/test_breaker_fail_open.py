import time

from redis.asyncio import Redis

from gateway.observability.metrics import REDIS_ERRORS
from gateway.routing.breaker import BreakerConfig, RedisCircuitBreaker


async def test_breaker_fails_open_quickly_when_redis_is_down() -> None:
    # Nothing listens on port 1: connection refused immediately.
    redis = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2, socket_timeout=0.2)
    breaker = RedisCircuitBreaker(redis, BreakerConfig())
    before = REDIS_ERRORS.labels(component="breaker")._value.get()
    started = time.perf_counter()
    try:
        assert await breaker.allow("groq") is True
        assert await breaker.record_failure("groq") is False
        await breaker.record_success("groq")
    finally:
        await redis.aclose()
    assert time.perf_counter() - started < 3
    # record_success without a held probe doesn't touch Redis at all (closed circuit).
    assert REDIS_ERRORS.labels(component="breaker")._value.get() - before == 2
