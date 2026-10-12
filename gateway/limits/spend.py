"""Monthly spend caps with reserve-then-settle, enforced atomically in Redis.

reserve(): before calling upstream, atomically check  spent + reserved + worst_case <= cap
           and hold `worst_case`. Concurrent requests see each other's reservations, so N
           parallel requests can never jointly overshoot the cap.
settle():  after the request (success, error, or client disconnect), release the reservation
           and add the actual cost.
Settle is idempotent and retried in the background if Redis blips, so a served request's cost
always reaches the total while its (worst-case) reservation still holds the budget.
Reservations expire, so a crashed replica can't leak budget forever. Redis is the hot path;
Postgres (`requests.cost_usd`) is the durable ledger used to rebuild the Redis total if it's
missing. Fails CLOSED on Redis errors. See docs/decisions/0009.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import LimiterUnavailable, SpendCapExceeded
from gateway.observability.metrics import REDIS_ERRORS, SPEND_SETTLE_FAILURES

log = logging.getLogger(__name__)
_LUA = Path(__file__).parent / "lua"
_NANOS_PER_USD = Decimal(10**9)
_KEY_TTL_MS = 40 * 24 * 3600 * 1000  # outlives the month it belongs to
# Background settle retries after a Redis error (~35 s in total). The reservation must outlive
# request + retries, which Settings enforces (spend_reservation_ttl_s).
SETTLE_RETRY_DELAYS_S = (0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 15.0)


def to_nanos(usd: Decimal, *, round_up: bool) -> int:
    """Integer nano-dollars: exact in Redis (INCRBY) and Lua, no float rounding of money."""
    rounding = ROUND_CEILING if round_up else ROUND_HALF_UP
    return int((usd * _NANOS_PER_USD).to_integral_value(rounding=rounding))


def from_nanos(nanos: int) -> Decimal:
    return Decimal(nanos) / _NANOS_PER_USD


def month_bounds(now: datetime) -> tuple[str, datetime, datetime]:
    """('2026-10', start, end) for the UTC calendar month containing `now`."""
    start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = (
        start.replace(year=start.year + 1, month=1)
        if start.month == 12
        else start.replace(month=start.month + 1)
    )
    return start.strftime("%Y-%m"), start, end


@dataclass(frozen=True)
class Reservation:
    key_id: uuid.UUID
    month: str
    request_id: str
    amount_nanos: int


class SpendLedger(Protocol):
    """Durable record of settled spend (Postgres)."""

    async def spent_between(self, key_id: uuid.UUID, start: datetime, end: datetime) -> Decimal: ...


class SpendLimiter(Protocol):
    async def reserve(
        self, key: ApiKeyRecord, request_id: str, estimate_usd: Decimal
    ) -> Reservation | None:
        """Hold the estimate, or raise SpendCapExceeded. None if the key has no cap."""
        ...

    async def settle(self, reservation: Reservation, actual_usd: Decimal) -> None:
        """Never raises: a response that was already served must not fail on bookkeeping."""
        ...

    async def remaining(self, key: ApiKeyRecord) -> Decimal | None:
        """Cap minus settled minus reserved, or None if uncapped/unknown. Advisory only
        (used for routing); enforcement is always the atomic reserve()."""
        ...


class RedisSpendLimiter:
    def __init__(
        self,
        redis: Redis,
        ledger: SpendLedger,
        reservation_ttl_s: float,
        prefix: str = "spend",
        retry_delays_s: tuple[float, ...] = SETTLE_RETRY_DELAYS_S,
    ) -> None:
        self._redis = redis
        self._retry_delays_s = retry_delays_s
        self._retries: set[asyncio.Task[None]] = set()
        self._ledger = ledger
        self._reservation_ttl_ms = int(reservation_ttl_s * 1000)
        self._prefix = prefix
        self._reserve = redis.register_script((_LUA / "spend_reserve.lua").read_text())
        self._settle = redis.register_script((_LUA / "spend_settle.lua").read_text())

    def _keys(self, key_id: uuid.UUID, month: str) -> list[str]:
        base = f"{self._prefix}:{{{key_id}:{month}}}"
        return [f"{base}:spent", f"{base}:res", f"{base}:res_exp", f"{base}:reserved"]

    async def reserve(
        self, key: ApiKeyRecord, request_id: str, estimate_usd: Decimal
    ) -> Reservation | None:
        cap = key.monthly_spend_cap_usd
        if cap is None:
            return None
        month, start, end = month_bounds(datetime.now(UTC))
        keys = self._keys(key.id, month)
        amount = to_nanos(estimate_usd, round_up=True)
        args: list[int | str] = [
            to_nanos(cap, round_up=False),
            request_id,
            amount,
            self._reservation_ttl_ms,
            _KEY_TTL_MS,
        ]
        try:
            status, spent, _ = (int(v) for v in await self._reserve(keys=keys, args=args))
            if status == -1:
                await self._hydrate(key.id, keys[0], start, end)
                status, spent, _ = (int(v) for v in await self._reserve(keys=keys, args=args))
        except RedisError as exc:
            REDIS_ERRORS.labels(component="spend_limiter").inc()
            raise LimiterUnavailable("Spend limiter unavailable; try again shortly.") from exc
        if status != 1:
            raise SpendCapExceeded(
                f"Monthly spend cap reached: ${from_nanos(spent):.6f} spent of ${cap} "
                f"(this request could cost up to ${estimate_usd:.6f})."
            )
        return Reservation(key.id, month, request_id, amount)

    async def _hydrate(
        self, key_id: uuid.UUID, spent_key: str, start: datetime, end: datetime
    ) -> None:
        """First request of the month (or after Redis lost data): rebuild from Postgres."""
        try:
            spent = await self._ledger.spent_between(key_id, start, end)
        except Exception as exc:
            raise LimiterUnavailable("Spend ledger unavailable; try again shortly.") from exc
        # NX: if another replica hydrated first, keep its value (and anything settled since).
        await self._redis.set(spent_key, to_nanos(spent, round_up=False), nx=True, px=_KEY_TTL_MS)

    async def remaining(self, key: ApiKeyRecord) -> Decimal | None:
        cap = key.monthly_spend_cap_usd
        if cap is None:
            return None
        month, _, _ = month_bounds(datetime.now(UTC))
        spent_key, _, _, reserved_key = self._keys(key.id, month)
        try:
            spent, reserved = await self._redis.mget(spent_key, reserved_key)
        except RedisError:
            REDIS_ERRORS.labels(component="spend_limiter").inc()
            return None
        if spent is None:
            return None  # not hydrated yet this month: unknown
        return cap - from_nanos(int(spent) + int(reserved or 0))

    async def settle(self, reservation: Reservation, actual_usd: Decimal) -> None:
        if await self._try_settle(reservation, actual_usd):
            return
        # Don't hold the response for retries. Until settled, the reservation (>= the actual
        # cost) keeps that budget held, so the cap stays safe while we retry.
        task = asyncio.create_task(self._settle_with_retries(reservation, actual_usd))
        self._retries.add(task)
        task.add_done_callback(self._retries.discard)

    async def _try_settle(self, reservation: Reservation, actual_usd: Decimal) -> bool:
        keys = self._keys(reservation.key_id, reservation.month)
        args: list[int | str] = [reservation.request_id, to_nanos(actual_usd, round_up=False)]
        try:
            await self._settle(keys=keys, args=args)  # idempotent: safe to retry
        except RedisError:
            REDIS_ERRORS.labels(component="spend_limiter").inc()
            return False
        return True

    async def _settle_with_retries(self, reservation: Reservation, actual_usd: Decimal) -> None:
        for delay in self._retry_delays_s:
            await asyncio.sleep(delay)
            if await self._try_settle(reservation, actual_usd):
                return
        # The reservation will expire and this request's cost never reaches the Redis total
        # (Postgres still has it). Count it: this under-counts the key's spend.
        SPEND_SETTLE_FAILURES.inc()
        log.error(
            "could not settle reservation %s ($%s) after retries",
            reservation.request_id,
            actual_usd,
        )

    async def close(self) -> None:
        """On shutdown, give pending settle retries a last chance to land."""
        if self._retries:
            await asyncio.wait(set(self._retries), timeout=5)
