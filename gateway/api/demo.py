"""Public demo keys for the playground: tiny spend cap, low rate limits, cheap models only.

Anyone can mint one, so creation itself is capped globally per hour (a Redis counter shared by
all replicas). The worst case is bounded: demo_keys_per_hour x demo_spend_cap_usd per hour.
See docs/decisions/0013.
"""

import secrets
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter
from redis.exceptions import RedisError

from gateway.api.deps import ContainerDep
from gateway.core.errors import LimiterUnavailable, NotFound, RateLimitExceeded

router = APIRouter(prefix="/demo")


async def _claim_quota(container: ContainerDep) -> None:
    redis = container.redis
    if redis is None:
        raise LimiterUnavailable("Demo keys need Redis.")
    hour = datetime.now(UTC).strftime("%Y%m%d%H")
    try:
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(f"demo:keys:{hour}")
            pipe.expire(f"demo:keys:{hour}", 3600)
            issued, _ = await pipe.execute()
    except RedisError as exc:
        raise LimiterUnavailable("Demo key quota unavailable; try again shortly.") from exc
    if int(issued) > container.settings.demo_keys_per_hour:
        raise RateLimitExceeded(
            "The demo key quota for this hour is used up. Try again later.", {"Retry-After": "600"}
        )


@router.post("/keys", status_code=201)
async def create_demo_key(container: ContainerDep) -> dict[str, Any]:
    settings = container.settings
    if not settings.demo_enabled or container.admin_keys is None:
        raise NotFound("Demo keys are not enabled on this gateway.")
    await _claim_quota(container)
    known = {spec.alias for spec in container.registry.all()} | {"auto"}
    models = [m.strip() for m in settings.demo_models.split(",") if m.strip() in known]
    raw, record = await container.admin_keys.create(
        f"demo-{secrets.token_hex(3)}",
        rpm_limit=settings.demo_rpm,
        tpm_limit=settings.demo_tpm,
        monthly_spend_cap_usd=settings.demo_spend_cap_usd,
        allowed_models=models,
    )
    return {
        "key": raw,
        "name": record.name,
        "rpm_limit": settings.demo_rpm,
        "tpm_limit": settings.demo_tpm,
        "monthly_spend_cap_usd": str(Decimal(settings.demo_spend_cap_usd)),
        "allowed_models": models,
    }
