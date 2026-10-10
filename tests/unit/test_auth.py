import pytest

from gateway.core.auth import (
    ApiKeyRecord,
    CachedKeyStore,
    bearer_token,
    generate_key,
    hash_key,
)
from gateway.core.errors import KeyStoreUnavailable
from tests.helpers import FakeKeyStore, key_record


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_generated_keys_are_prefixed_and_unique() -> None:
    keys = {generate_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(k.startswith("gw-") and len(k) > 40 for k in keys)


def test_hash_is_sha256_hex() -> None:
    assert len(hash_key("gw-x")) == 64
    assert hash_key("gw-x") == hash_key("gw-x") != hash_key("gw-y")


def test_bearer_token_parsing() -> None:
    assert bearer_token("Bearer gw-abc") == "gw-abc"
    assert bearer_token("bearer   gw-abc ") == "gw-abc"
    assert bearer_token("Basic xyz") is None
    assert bearer_token("Bearer ") is None
    assert bearer_token(None) is None


async def test_cache_hits_skip_the_store_until_ttl_expires() -> None:
    inner, clock = FakeKeyStore(), FakeClock()
    inner.records["h"] = key_record()
    cache = CachedKeyStore(inner, ttl_s=60, max_entries=10, clock=clock)

    assert await cache.get_by_hash("h") is not None
    assert await cache.get_by_hash("h") is not None
    assert inner.lookups == 1

    clock.now = 61
    await cache.get_by_hash("h")
    assert inner.lookups == 2


async def test_revocation_visible_after_ttl() -> None:
    inner, clock = FakeKeyStore(), FakeClock()
    inner.records["h"] = key_record()
    cache = CachedKeyStore(inner, ttl_s=60, max_entries=10, clock=clock)
    await cache.get_by_hash("h")

    inner.records["h"] = key_record(active=False)
    cached = await cache.get_by_hash("h")
    assert cached is not None and cached.active  # stale, by design, within the TTL
    clock.now = 60.5
    refreshed = await cache.get_by_hash("h")
    assert refreshed is not None and not refreshed.active


async def test_misses_are_not_cached() -> None:
    inner = FakeKeyStore()
    cache = CachedKeyStore(inner, ttl_s=60, max_entries=10)
    assert await cache.get_by_hash("new") is None
    inner.records["new"] = key_record()
    assert await cache.get_by_hash("new") is not None  # newly created key works immediately


async def test_cache_is_bounded() -> None:
    inner = FakeKeyStore()
    for i in range(5):
        inner.records[str(i)] = key_record()
    cache = CachedKeyStore(inner, ttl_s=60, max_entries=2)
    for i in range(5):
        await cache.get_by_hash(str(i))
    inner.lookups = 0
    await cache.get_by_hash("4")
    await cache.get_by_hash("0")  # evicted
    assert inner.lookups == 1


class FlakyStore(FakeKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.down = False

    async def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        if self.down:
            raise ConnectionRefusedError("postgres down")
        return await super().get_by_hash(key_hash)


async def test_known_keys_keep_working_while_db_is_down() -> None:
    inner, clock = FlakyStore(), FakeClock()
    inner.records["h"] = key_record()
    cache = CachedKeyStore(inner, ttl_s=60, max_entries=10, clock=clock, stale_if_error_s=600)
    await cache.get_by_hash("h")

    inner.down = True
    clock.now = 120  # entry expired 60s ago
    assert await cache.get_by_hash("h") is not None  # stale-if-error

    clock.now = 60 + 601  # past the stale window
    with pytest.raises(KeyStoreUnavailable):
        await cache.get_by_hash("h")


async def test_unknown_key_while_db_down_is_503_not_401() -> None:
    inner = FlakyStore()
    inner.down = True
    with pytest.raises(KeyStoreUnavailable):
        await CachedKeyStore(inner, ttl_s=60, max_entries=10).get_by_hash("never-seen")
