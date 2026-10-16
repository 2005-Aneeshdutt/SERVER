"""Long-lived dependencies, built once per process in the app lifespan.

Tests build a Container directly with fakes (in-memory key store, mocked upstream HTTP).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.cache.response_cache import RedisResponseCache, ResponseCache
from gateway.capture.producer import CaptureProducer
from gateway.core import tokens
from gateway.core.auth import CachedKeyStore, KeyStore
from gateway.core.config import Settings
from gateway.db.repositories import (
    SqlDistillAudit,
    SqlKeyStore,
    SqlRequestLogSink,
    SqlSpendLedger,
    SqlUsageReader,
)
from gateway.db.session import make_engine, make_sessionmaker
from gateway.distill.rollout import RolloutConfig, RolloutController
from gateway.limits.ratelimit import RateLimiter, RedisRateLimiter
from gateway.limits.spend import RedisSpendLimiter, SpendLimiter
from gateway.observability.request_log import RequestLogWriter
from gateway.providers.base import Provider
from gateway.providers.factory import build_providers, make_http_client
from gateway.routing.breaker import Breaker, BreakerConfig, RedisCircuitBreaker
from gateway.routing.dispatch import Dispatcher, RetryPolicy
from gateway.routing.registry import ModelRegistry
from gateway.routing.smart import RouterModel, SmartRouter

log = logging.getLogger(__name__)
ReadinessCheck = Callable[[], Awaitable[None]]


@dataclass
class Container:
    settings: Settings
    registry: ModelRegistry
    providers: Mapping[str, Provider]
    keys: KeyStore
    request_log: RequestLogWriter
    breaker: Breaker
    dispatcher: Dispatcher
    rate_limiter: RateLimiter
    spend: SpendLimiter
    cache: ResponseCache
    admin_keys: SqlKeyStore | None = None  # None in unit tests (admin API then 503s)
    usage_reader: SqlUsageReader | None = None
    router: SmartRouter | None = None  # model="auto"; None if the router isn't installed
    redis: Redis | None = None  # shared state for small counters (demo-key quota)
    capture: CaptureProducer | None = None  # distillation capture; None unless enabled
    rollout: RolloutController | None = None  # distilled-model rollout; None unless enabled
    distill_audit: SqlDistillAudit | None = None
    # name -> coroutine that raises if the dependency is unhealthy (used by /readyz)
    readiness: dict[str, ReadinessCheck] = field(default_factory=dict)
    closers: list[Callable[[], Awaitable[None]]] = field(default_factory=list)


def make_dispatcher(
    settings: Settings,
    registry: ModelRegistry,
    providers: Mapping[str, Provider],
    breaker: Breaker,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Dispatcher:
    policy = RetryPolicy(
        max_attempts_per_provider=settings.retry_max_attempts_per_provider,
        base_delay_s=settings.retry_base_delay_s,
        max_delay_s=settings.retry_max_delay_s,
    )
    return Dispatcher(
        registry,
        providers,
        breaker,
        policy=policy,
        first_token_timeout_s=settings.first_token_timeout_s,
        total_timeout_s=settings.total_timeout_s,
        sleep=sleep,
    )


def make_redis(settings: Settings) -> Redis:
    # Short timeouts: a hung Redis must not stall requests (the breaker fails open on error).
    return Redis.from_url(
        settings.redis_url,
        socket_connect_timeout=settings.redis_timeout_s,
        socket_timeout=settings.redis_timeout_s,
    )


def _breaker_config(settings: Settings) -> BreakerConfig:
    return BreakerConfig(
        failure_threshold=settings.breaker_failure_threshold,
        window_s=settings.breaker_window_s,
        cooldown_s=settings.breaker_cooldown_s,
        probe_ttl_s=settings.breaker_probe_ttl_s,
    )


async def build_container(settings: Settings) -> Container:
    registry = ModelRegistry.from_yaml(settings.models_file)
    http_client = make_http_client(settings)
    engine = make_engine(settings.database_url)
    sessions = make_sessionmaker(engine)
    replica_engine = make_engine(settings.database_read_url) if settings.database_read_url else None
    replica_sessions = make_sessionmaker(replica_engine) if replica_engine else None
    redis = make_redis(settings)
    providers = build_providers(settings, http_client)
    breaker = RedisCircuitBreaker(redis, _breaker_config(settings))
    key_store = SqlKeyStore(sessions)
    await asyncio.to_thread(tokens.warm_up)
    router = await build_router(settings, registry, breaker)
    spend = RedisSpendLimiter(redis, SqlSpendLedger(sessions), settings.spend_reservation_ttl_s)
    audit = SqlDistillAudit(sessions)
    rollout = _rollout(settings, redis, registry, providers, audit)
    return Container(
        settings=settings,
        registry=registry,
        providers=providers,
        keys=CachedKeyStore(key_store, settings.key_cache_ttl_s, settings.key_cache_max_entries),
        request_log=RequestLogWriter(
            SqlRequestLogSink(engine), settings.log_queue_size, settings.log_batch_size
        ),
        breaker=breaker,
        dispatcher=make_dispatcher(settings, registry, providers, breaker),
        rate_limiter=RedisRateLimiter(redis),
        spend=spend,
        cache=RedisResponseCache(
            redis, settings.cache_ttl_s, lock_ttl_s=settings.total_timeout_s + 5
        ),
        admin_keys=key_store,
        usage_reader=SqlUsageReader(sessions, replica_sessions),
        router=router,
        redis=redis,
        capture=_capture(settings),
        rollout=rollout,
        distill_audit=audit,
        readiness={
            "redis": _redis_check(redis),
            "postgres": _postgres_check(engine),
            **({"postgres_replica": _postgres_check(replica_engine)} if replica_engine else {}),
        },
        closers=[
            spend.close,  # before redis.aclose: pending settle retries need Redis
            *([rollout.close] if rollout else []),
            http_client.aclose,
            engine.dispose,
            redis.aclose,
            *([replica_engine.dispose] if replica_engine else []),
            *([_closer(router.close)] if router else []),
        ],
    )


def _rollout(
    settings: Settings,
    redis: Redis,
    registry: ModelRegistry,
    providers: Mapping[str, Provider],
    audit: SqlDistillAudit,
) -> RolloutController | None:
    if not settings.distill_rollout_enabled:
        return None
    config = RolloutConfig(
        window_minutes=settings.distill_window_minutes,
        min_samples=settings.distill_min_samples,
        failure_threshold=settings.distill_failure_threshold,
        shadow_concurrency=settings.distill_shadow_concurrency,
        audit_rate=settings.distill_audit_rate,
        audit_min_samples=settings.distill_audit_min_samples,
        audit_threshold=settings.distill_audit_threshold,
    )
    return RolloutController(redis, registry, providers, audit.write, config)


def _capture(settings: Settings) -> CaptureProducer | None:
    if not settings.capture_enabled:
        return None
    from gateway.capture.kafka import KafkaSender  # needs the `capture` extra

    return CaptureProducer(
        KafkaSender(settings.kafka_bootstrap), settings.capture_topic, settings.capture_queue_size
    )


async def build_router(
    settings: Settings, registry: ModelRegistry, breaker: Breaker
) -> SmartRouter | None:
    """model="auto" is enabled only if the classifier artifact exists and the optional
    `router` extra (torch + sentence-transformers) is installed."""
    path = Path(settings.router_model_path)
    if not await asyncio.to_thread(path.exists):
        log.warning("model=auto disabled: no router model at %s", path)
        return None
    try:
        from gateway.routing.embedder import BgeEmbedder

        embedder = await asyncio.to_thread(BgeEmbedder, threads=settings.router_threads)
    except ImportError:
        log.warning("model=auto disabled: install the 'router' extra")
        return None
    router = SmartRouter(
        RouterModel.load(path), embedder, registry, breaker, settings.default_max_tokens
    )
    await router.warm_up()
    return router


def _closer(close: Callable[[], None]) -> Callable[[], Awaitable[None]]:
    async def run() -> None:
        close()

    return run


def _redis_check(redis: Redis) -> ReadinessCheck:
    async def check() -> None:
        await redis.ping()

    return check


def _postgres_check(engine: AsyncEngine) -> ReadinessCheck:
    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))

    return check
