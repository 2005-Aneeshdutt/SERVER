"""Per-key RPM + TPM limits: token buckets in Redis, one atomic Lua call per request.

TPM is charged up front with an estimate (prompt tokens + max output tokens) and corrected
with `adjust_tokens` once the real usage is known. Fails CLOSED: if Redis is down we can't
enforce limits, so we reject (503). See docs/decisions/0008.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.core.auth import ApiKeyRecord
from gateway.core.errors import LimiterUnavailable
from gateway.observability.metrics import REDIS_ERRORS

log = logging.getLogger(__name__)
_LUA = Path(__file__).parent / "lua"


@dataclass(frozen=True)
class RateDecision:
    allowed: bool
    rpm_limit: int | None = None
    rpm_remaining: int | None = None
    tpm_limit: int | None = None
    tpm_remaining: int | None = None
    retry_after_s: float | None = None

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.rpm_limit is not None and self.rpm_remaining is not None:
            headers["X-RateLimit-Limit"] = str(self.rpm_limit)
            headers["X-RateLimit-Remaining"] = str(max(0, self.rpm_remaining))
        if self.tpm_limit is not None and self.tpm_remaining is not None:
            headers["X-RateLimit-Limit-Tokens"] = str(self.tpm_limit)
            headers["X-RateLimit-Remaining-Tokens"] = str(max(0, self.tpm_remaining))
        if self.retry_after_s is not None:
            headers["Retry-After"] = str(max(1, round(self.retry_after_s + 0.5)))
        return headers


UNLIMITED = RateDecision(allowed=True)


class RateLimiter(Protocol):
    async def acquire(self, key: ApiKeyRecord, tokens: int) -> RateDecision: ...
    async def adjust_tokens(self, key: ApiKeyRecord, delta: int) -> None:
        """delta > 0 refunds tokens (estimate was too high), delta < 0 charges more."""
        ...


class RedisRateLimiter:
    def __init__(self, redis: Redis, prefix: str = "rl") -> None:
        self._prefix = prefix
        self._acquire = redis.register_script((_LUA / "ratelimit_acquire.lua").read_text())
        self._adjust = redis.register_script((_LUA / "ratelimit_adjust.lua").read_text())

    def _keys(self, key: ApiKeyRecord) -> list[str]:
        base = f"{self._prefix}:{{{key.id}}}"
        return [f"{base}:rpm", f"{base}:tpm"]

    async def acquire(self, key: ApiKeyRecord, tokens: int) -> RateDecision:
        if key.rpm_limit is None and key.tpm_limit is None:
            return UNLIMITED
        args = [key.rpm_limit or 0, key.tpm_limit or 0, max(0, tokens)]
        try:
            raw = await self._acquire(keys=self._keys(key), args=args)
        except RedisError as exc:
            REDIS_ERRORS.labels(component="rate_limiter").inc()
            raise LimiterUnavailable("Rate limiter unavailable; try again shortly.") from exc
        allowed, rpm_left, tpm_left, retry_ms = (int(v) for v in raw)
        return RateDecision(
            allowed=bool(allowed),
            rpm_limit=key.rpm_limit,
            rpm_remaining=rpm_left if key.rpm_limit else None,
            tpm_limit=key.tpm_limit,
            tpm_remaining=tpm_left if key.tpm_limit else None,
            retry_after_s=retry_ms / 1000 if not allowed else None,
        )

    async def adjust_tokens(self, key: ApiKeyRecord, delta: int) -> None:
        if not key.tpm_limit or delta == 0:
            return
        try:
            await self._adjust(keys=self._keys(key)[1:], args=[key.tpm_limit, delta])
        except RedisError:
            # The request already happened; the bucket just stays at the estimate. Not fatal.
            REDIS_ERRORS.labels(component="rate_limiter").inc()
            log.warning("could not adjust TPM bucket for key %s", key.id)
