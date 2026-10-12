import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import respx
from fastapi import FastAPI
from pydantic import SecretStr

from gateway.app import create_app
from gateway.container import Container, make_dispatcher
from gateway.core.auth import ApiKeyRecord, hash_key
from gateway.core.config import Settings
from gateway.observability.request_log import RequestLogWriter, RequestRecord
from gateway.providers.factory import build_providers
from gateway.routing.registry import ModelRegistry
from tests.helpers import (
    FakeBreaker,
    FakeKeyStore,
    FakeRateLimiter,
    FakeSpend,
    MemoryCache,
    MemorySink,
    key_record,
    no_sleep,
)

ROOT = Path(__file__).resolve().parents[1]
API_KEY = "gw-test-key-0123456789"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        models_file=str(ROOT / "models.yaml"),
        groq_api_key=SecretStr("groq-secret"),
        gemini_api_key=SecretStr("gemini-secret"),
        openai_api_key=SecretStr("openai-secret"),
        anthropic_api_key=SecretStr("anthropic-secret"),
        openrouter_api_key=SecretStr("openrouter-secret"),
        admin_token=SecretStr("test-admin-token"),
        first_token_timeout_s=2,
        total_timeout_s=5,
    )


@pytest.fixture
def upstream() -> Iterator[respx.Router]:
    """Mocked provider HTTP. Any request without a matching route fails the test."""
    router = respx.Router(assert_all_called=False)
    yield router


@pytest.fixture
def http_client(upstream: respx.Router) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(upstream.async_handler))


@dataclass
class Gateway:
    app: FastAPI
    client: httpx.AsyncClient
    container: Container
    keys: FakeKeyStore
    sink: MemorySink
    key: ApiKeyRecord
    breaker: FakeBreaker
    rate: FakeRateLimiter
    spend: FakeSpend
    cache: MemoryCache

    async def logged(self, count: int = 1) -> list[RequestRecord]:
        """Wait for the background writer to persist `count` records."""
        for _ in range(200):
            if len(self.sink.records) >= count:
                return self.sink.records
            await asyncio.sleep(0.01)
        raise AssertionError(f"expected {count} log records, got {len(self.sink.records)}")


@pytest.fixture
async def gateway(settings: Settings, http_client: httpx.AsyncClient) -> AsyncIterator[Gateway]:
    keys = FakeKeyStore()
    record = key_record()
    keys.records[hash_key(API_KEY)] = record
    sink = MemorySink()
    registry = ModelRegistry.from_yaml(settings.models_file)
    providers = build_providers(settings, http_client)
    breaker = FakeBreaker()
    rate, spend, cache = FakeRateLimiter(), FakeSpend(), MemoryCache()
    container = Container(
        settings=settings,
        registry=registry,
        providers=providers,
        keys=keys,
        request_log=RequestLogWriter(sink, max_queue=100, batch_size=50),
        breaker=breaker,
        dispatcher=make_dispatcher(settings, registry, providers, breaker, sleep=no_sleep),
        rate_limiter=rate,
        spend=spend,
        cache=cache,
        closers=[http_client.aclose],
    )

    async def factory() -> Container:
        return container

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        headers = {"Authorization": f"Bearer {API_KEY}"}
        async with httpx.AsyncClient(
            transport=transport, base_url="http://gateway", headers=headers
        ) as client:
            yield Gateway(app, client, container, keys, sink, record, breaker, rate, spend, cache)
