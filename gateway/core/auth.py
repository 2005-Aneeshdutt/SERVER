"""Client API keys: generation, hashing, and a TTL-cached lookup.

Keys are 256-bit random tokens, so a plain SHA-256 is enough (no dictionary to attack, unlike
passwords) and it's fast enough to run on every request. See docs/decisions/0002.
"""

import hashlib
import secrets
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from gateway.core.errors import KeyStoreUnavailable
from gateway.observability.metrics import KEY_STORE_STALE

KEY_PREFIX = "gw-"


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    id: uuid.UUID
    name: str
    rpm_limit: int | None
    tpm_limit: int | None
    monthly_spend_cap_usd: Decimal | None
    allowed_models: frozenset[str] | None  # None = every alias
    active: bool
    # model="auto": "low" saves the most, "high" sends more traffic to the strong model.
    routing_quality: str = "balanced"
    # Distillation (default off): may this key's prompts/responses be captured for training
    # (PII-scrubbed)? Is its traffic simulated (replayer, load tests)? See PLAN.md.
    distill_opt_in: bool = False
    simulated: bool = False
    # May this key be served by a distilled model during a rollout (M10)? A separate consent
    # from contributing training data.
    distill_serving: bool = False

    def may_use(self, alias: str) -> bool:
        return self.allowed_models is None or alias in self.allowed_models


class KeyStore(Protocol):
    async def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None: ...


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


class CachedKeyStore:
    """In-process TTL cache in front of the DB. Revocations take effect within `ttl_s`.

    Only hits are cached: misses (unknown keys) always go to the DB, so a key created a moment
    ago works immediately, and random-key spam can't fill the cache. Bounded with LRU eviction.

    Stale-if-error: if the DB lookup *fails* (Postgres down), an entry that expired less than
    `stale_if_error_s` ago is still served, so known keys keep working through a DB outage.
    Unknown keys get 503: we can't tell valid from invalid. See docs/decisions/0011.
    """

    def __init__(
        self,
        inner: KeyStore,
        ttl_s: float,
        max_entries: int,
        clock: "Clock" = time.monotonic,
        stale_if_error_s: float = 3600.0,
    ) -> None:
        self._inner = inner
        self._ttl_s = ttl_s
        self._stale_if_error_s = stale_if_error_s
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, ApiKeyRecord]] = OrderedDict()

    def evict(self, key_id: uuid.UUID) -> None:
        """Drop a key from *this replica's* cache (after an admin update). Other replicas
        pick the change up within the TTL."""
        for key_hash, (_, record) in list(self._entries.items()):
            if record.id == key_id:
                del self._entries[key_hash]

    async def get_by_hash(self, key_hash: str) -> ApiKeyRecord | None:
        now = self._clock()
        cached = self._entries.get(key_hash)
        if cached is not None and cached[0] > now:
            self._entries.move_to_end(key_hash)
            return cached[1]
        try:
            record = await self._inner.get_by_hash(key_hash)
        except Exception as exc:
            # cached[0] is the expiry time; staleness = time since it expired.
            if cached is not None and now - cached[0] < self._stale_if_error_s:
                KEY_STORE_STALE.inc()
                return cached[1]
            raise KeyStoreUnavailable("Key store unavailable; try again shortly.") from exc
        if record is None:
            self._entries.pop(key_hash, None)
            return None
        self._entries[key_hash] = (now + self._ttl_s, record)
        self._entries.move_to_end(key_hash)
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)
        return record


class Clock(Protocol):
    def __call__(self) -> float: ...
