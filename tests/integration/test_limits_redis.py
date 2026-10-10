import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from decimal import Decimal

import pytest
from redis.asyncio import Redis

from gateway.core.auth import ApiKeyRecord
from gateway.core.config import Settings
from gateway.core.errors import SpendCapExceeded
from gateway.limits.ratelimit import RedisRateLimiter
from gateway.limits.spend import RedisSpendLimiter, from_nanos

pytestmark = pytest.mark.integration


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(Settings().redis_url)
    yield client
    await client.aclose()


def record(**limits: object) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=uuid.uuid4(),
        name="t",
        rpm_limit=limits.get("rpm"),  # type: ignore[arg-type]
        tpm_limit=limits.get("tpm"),  # type: ignore[arg-type]
        monthly_spend_cap_usd=limits.get("cap"),  # type: ignore[arg-type]
        allowed_models=None,
        active=True,
    )


class Ledger:
    def __init__(self, spent: Decimal) -> None:
        self.spent = spent
        self.calls = 0

    async def spent_between(self, key_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
        self.calls += 1
        return self.spent


# ---- rate limiter --------------------------------------------------------------------------


async def test_bucket_refills_continuously(redis: Redis) -> None:
    limiter, key = RedisRateLimiter(redis), record(rpm=60)  # 1 per second
    burst = await asyncio.gather(*(limiter.acquire(key, 0) for _ in range(70)))
    assert sum(d.allowed for d in burst) == 60
    denied = next(d for d in burst if not d.allowed)
    assert denied.retry_after_s is not None and 0 < denied.retry_after_s <= 1.0
    # Margins must exceed clock jumps: Docker Desktop's VM clock steps back up to ~1s.
    await asyncio.sleep(3.0)
    assert (await limiter.acquire(key, 0)).allowed


async def test_tpm_refund_makes_room_for_more_requests(redis: Redis) -> None:
    limiter, key = RedisRateLimiter(redis), record(tpm=1000)
    assert (await limiter.acquire(key, 900)).allowed
    assert not (await limiter.acquire(key, 900)).allowed
    await limiter.adjust_tokens(key, 850)  # actually used 50
    assert (await limiter.acquire(key, 900)).allowed


async def test_request_bigger_than_bucket_waits_for_full_bucket_then_runs(redis: Redis) -> None:
    limiter, key = RedisRateLimiter(redis), record(tpm=100)
    first = await limiter.acquire(key, 5000)
    assert first.allowed and first.tpm_remaining is not None and first.tpm_remaining < 0  # debt
    assert not (await limiter.acquire(key, 1)).allowed


async def test_backward_clock_jump_never_removes_tokens(redis: Redis) -> None:
    """Simulate the clock stepping back: a stored timestamp in the future."""
    limiter, key = RedisRateLimiter(redis), record(rpm=60)
    rpm_key = limiter._keys(key)[0]
    seconds, micros = await redis.time()
    future_ms = seconds * 1000 + micros // 1000 + 5000
    await redis.hset(rpm_key, mapping={"tokens": 10, "ts": future_ms})
    decision = await limiter.acquire(key, 0)
    assert decision.allowed and decision.rpm_remaining == 9  # not 10 - 5s*1/s - 1 = 4


# ---- spend limiter -------------------------------------------------------------------------


async def test_first_use_hydrates_month_spend_from_ledger(redis: Redis) -> None:
    ledger = Ledger(Decimal("0.90"))
    limiter, key = RedisSpendLimiter(redis, ledger, 60), record(cap=Decimal(1))
    assert await limiter.reserve(key, "r1", Decimal("0.05")) is not None
    with pytest.raises(SpendCapExceeded):
        await limiter.reserve(key, "r2", Decimal("0.06"))  # 0.90 + 0.05 + 0.06 > 1
    assert ledger.calls == 1  # hydrated once; afterwards Redis is the source of truth


async def test_settle_adds_actual_and_releases_reservation(redis: Redis) -> None:
    limiter, key = RedisSpendLimiter(redis, Ledger(Decimal(0)), 60), record(cap=Decimal(1))
    res = await limiter.reserve(key, "r1", Decimal("0.80"))
    assert res is not None
    with pytest.raises(SpendCapExceeded):
        await limiter.reserve(key, "r2", Decimal("0.30"))
    await limiter.settle(res, Decimal("0.10"))
    assert await limiter.reserve(key, "r2", Decimal("0.85")) is not None  # 0.10 + 0.85 <= 1
    spent = await redis.get(limiter._keys(key.id, res.month)[0])
    assert spent is not None and from_nanos(int(spent)) == Decimal("0.10")


async def test_crashed_requests_reservation_expires(redis: Redis) -> None:
    limiter, key = (
        RedisSpendLimiter(redis, Ledger(Decimal(0)), reservation_ttl_s=0.2),
        record(cap=Decimal(1)),
    )
    assert await limiter.reserve(key, "crashed", Decimal("0.99")) is not None  # never settled
    with pytest.raises(SpendCapExceeded):
        await limiter.reserve(key, "r2", Decimal("0.5"))
    await asyncio.sleep(3.0)  # > the ~1s backward clock steps seen on Docker Desktop
    assert await limiter.reserve(key, "r2", Decimal("0.5")) is not None


async def test_concurrent_reservations_never_exceed_cap(redis: Redis) -> None:
    ledger = Ledger(Decimal(0))
    limiters = [
        RedisSpendLimiter(Redis.from_url(Settings().redis_url), ledger, 60) for _ in range(5)
    ]
    key = record(cap=Decimal(1))
    results = await asyncio.gather(
        *(limiters[i % 5].reserve(key, f"r{i}", Decimal("0.07")) for i in range(100)),
        return_exceptions=True,
    )
    for limiter in limiters:
        await limiter._redis.aclose()
    assert sum(1 for r in results if not isinstance(r, BaseException)) == 14  # 1 // 0.07
    unexpected = [
        repr(r)
        for r in results
        if isinstance(r, BaseException) and not isinstance(r, SpendCapExceeded)
    ]
    assert not unexpected, unexpected


# ---- fail closed ---------------------------------------------------------------------------


async def test_limiters_fail_closed_when_redis_is_down() -> None:
    from gateway.core.errors import LimiterUnavailable

    dead = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.2)
    try:
        with pytest.raises(LimiterUnavailable):
            await RedisRateLimiter(dead).acquire(record(rpm=5), 1)
        with pytest.raises(LimiterUnavailable):
            await RedisSpendLimiter(dead, Ledger(Decimal(0)), 60).reserve(
                record(cap=Decimal(1)), "r", Decimal("0.1")
            )
        # Keys without limits never touch Redis, so they keep working.
        assert (await RedisRateLimiter(dead).acquire(record(), 1)).allowed
    finally:
        await dead.aclose()
