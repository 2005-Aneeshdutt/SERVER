"""Long-lived dependencies, built once per process in the app lifespan.

Tests build a Container directly with fakes (in-memory key store, mocked upstream HTTP).
"""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from gateway.cache.response_cache import RedisResponseCache, ResponseCache
from gateway.core import tokens
from gateway.core.auth import CachedKeyStore, KeyStore
from gateway.core.config import Settings
from gateway.db.repositories import (
    SqlKeyStore,
    SqlRequestLogSink,
    SqlSpendLedger,
    SqlUsageReader,
)
from gateway.db.session import make_engine, make_sessionmaker
from gateway.limits.ratelimit import RateLimiter, RedisRateLimiter
from gateway.limits.spend import RedisSpendLimiter, SpendLimiter
from gateway.observability.request_log import RequestLogWriter
from gateway.providers.base import Provider
from gateway.providers.factory import build_providers, make_http_client
from gateway.routing.breaker import Breaker, BreakerConfig, RedisCircuitBreaker
from gateway.routing.dispatch import Dispatcher, RetryPolicy
from gateway.routing.registry import ModelRegistry

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
        spend=RedisSpendLimiter(redis, SqlSpendLedger(sessions), settings.spend_reservation_ttl_s),
        cache=RedisResponseCache(
            redis, settings.cache_ttl_s, lock_ttl_s=settings.total_timeout_s + 5
        ),
        admin_keys=key_store,
        usage_reader=SqlUsageReader(sessions, replica_sessions),
        readiness={
            "redis": _redis_check(redis),
            "postgres": _postgres_check(engine),
            **({"postgres_replica": _postgres_check(replica_engine)} if replica_engine else {}),
        },
        closers=[
            http_client.aclose,
            engine.dispose,
            redis.aclose,
            *([replica_engine.dispose] if replica_engine else []),
        ],
    )


def _redis_check(redis: Redis) -> ReadinessCheck:
    async def check() -> None:
        await redis.ping()

    return check


def _postgres_check(engine: AsyncEngine) -> ReadinessCheck:
    async def check() -> None:
        async with engine.connect() as conn:
            await conn.execute(text("select 1"))

    return check
