"""Full gateway instances ("replicas") wired to the real Redis, with mocked upstream HTTP."""

import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack
from datetime import datetime
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr
from redis.asyncio import Redis

from gateway.app import create_app
from gateway.cache.response_cache import RedisResponseCache
from gateway.container import Container, make_dispatcher
from gateway.core.auth import ApiKeyRecord, hash_key
from gateway.core.config import Settings
from gateway.limits.ratelimit import RedisRateLimiter
from gateway.limits.spend import RedisSpendLimiter
from gateway.observability.request_log import RequestLogWriter
from gateway.providers.factory import build_providers
from gateway.routing.breaker import BreakerConfig, RedisCircuitBreaker
from gateway.routing.registry import ModelRegistry
from tests.conftest import ROOT
from tests.helpers import FakeKeyStore, MemorySink, no_sleep, warm_redis_pool


class ZeroLedger:
    async def spent_between(self, key_id: uuid.UUID, start: datetime, end: datetime) -> Decimal:
        return Decimal(0)


Replicas = Callable[[int, list[tuple[str, ApiKeyRecord]]], Awaitable[list[httpx.AsyncClient]]]


@pytest.fixture
async def replicas() -> AsyncIterator[tuple[Replicas, respx.Router]]:
    """Factory: start N gateway instances sharing one Redis; returns one HTTP client per instance.
    Each instance gets its *own* Redis connection pool, like separate processes would."""
    upstream = respx.Router(assert_all_called=False)
    settings = Settings(
        _env_file=None,
        models_file=str(ROOT / "models.yaml"),
        groq_api_key=SecretStr("groq-secret"),
        openrouter_api_key=SecretStr("or-secret"),
        gemini_api_key=SecretStr("g-secret"),
    )
    registry = ModelRegistry.from_yaml(settings.models_file)
    async with AsyncExitStack() as stack:

        async def start(
            count: int, keys: list[tuple[str, ApiKeyRecord]]
        ) -> list[httpx.AsyncClient]:
            clients = []
            for _ in range(count):
                redis = Redis.from_url(settings.redis_url)
                await warm_redis_pool(redis)
                stack.push_async_callback(redis.aclose)
                http = httpx.AsyncClient(transport=httpx.MockTransport(upstream.async_handler))
                providers = build_providers(settings, http)
                breaker = RedisCircuitBreaker(
                    redis, BreakerConfig(), prefix=f"cb-{uuid.uuid4().hex}"
                )
                key_store = FakeKeyStore()
                for raw, record in keys:
                    key_store.records[hash_key(raw)] = record
                container = Container(
                    settings=settings,
                    registry=registry,
                    providers=providers,
                    keys=key_store,
                    request_log=RequestLogWriter(MemorySink(), 10_000, 100),
                    breaker=breaker,
                    dispatcher=make_dispatcher(
                        settings, registry, providers, breaker, sleep=no_sleep
                    ),
                    rate_limiter=RedisRateLimiter(redis),
                    spend=RedisSpendLimiter(redis, ZeroLedger(), reservation_ttl_s=60),
                    cache=RedisResponseCache(redis, ttl_s=60, lock_ttl_s=30),
                    closers=[http.aclose],
                )

                async def factory(c: Container = container) -> Container:
                    return c

                app = create_app(factory)
                await stack.enter_async_context(app.router.lifespan_context(app))
                client = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://gw"
                )
                stack.push_async_callback(client.aclose)
                clients.append(client)
            return clients

        yield start, upstream
