"""Exact-match response cache in Redis, with request coalescing (stampede protection).

Cacheable when temperature == 0 or the client sends `X-Gateway-Cache: force`
(`X-Gateway-Cache: skip` opts out). The key covers every request field that can change the
output, and is scoped per API key. On a miss, one request (the leader) takes a short lock and
calls upstream; identical concurrent requests wait for its result instead of each paying for
their own call. Fails OPEN: if Redis is down we skip the cache. See docs/decisions/0010.
"""

import asyncio
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from redis.asyncio import Redis
from redis.exceptions import RedisError

from gateway.core.schemas import (
    ChatChunk,
    ChatRequest,
    ChatResponse,
    ChunkChoice,
    Delta,
    FunctionCallDelta,
    ToolCallDelta,
)
from gateway.observability.metrics import REDIS_ERRORS

log = logging.getLogger(__name__)

CACHE_HEADER = "x-gateway-cache"
# Fields that don't change what the model generates.
_NOT_IN_KEY = {"stream", "stream_options", "user"}
_UNLOCK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


def is_cacheable(body: ChatRequest, header: str | None) -> bool:
    directive = (header or "").strip().lower()
    if directive == "skip":
        return False
    return directive == "force" or body.temperature == 0


def cache_key(key_id: uuid.UUID, body: ChatRequest) -> str:
    """sha256 over the canonical JSON of the request (sorted keys, no whitespace)."""
    request = body.model_dump(mode="json", exclude_none=True, exclude=_NOT_IN_KEY)
    canonical = json.dumps(
        {"key": str(key_id), "request": request}, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


@dataclass(frozen=True)
class CachedEntry:
    response: dict[str, Any]  # ChatResponse JSON
    alias: str
    provider: str
    upstream_model: str
    cost_usd: str  # what the original call cost (= what each hit saves)
    latency_ms: int  # how long the original call took (= latency each hit saves)

    def chat_response(self) -> ChatResponse:
        return ChatResponse.model_validate(self.response)


class ResponseCache(Protocol):
    async def get(self, key: str) -> CachedEntry | None: ...
    async def set(self, key: str, entry: CachedEntry) -> None: ...
    async def try_lock(self, key: str) -> str | None:
        """Lock token if we became the leader for this key, else None."""
        ...

    async def unlock(self, key: str, token: str) -> None: ...


class RedisResponseCache:
    def __init__(
        self, redis: Redis, ttl_s: float, lock_ttl_s: float, prefix: str = "cache"
    ) -> None:
        self._redis = redis
        self._ttl_ms = int(ttl_s * 1000)
        self._lock_ttl_ms = int(lock_ttl_s * 1000)
        self._prefix = prefix
        self._unlock = redis.register_script(_UNLOCK)

    async def get(self, key: str) -> CachedEntry | None:
        try:
            raw = await self._redis.get(f"{self._prefix}:{key}")
        except RedisError:
            _redis_down("get")
            return None
        if raw is None:
            return None
        try:
            return CachedEntry(**json.loads(raw))
        except (ValueError, TypeError):
            return None  # written by an older, incompatible version: treat as a miss

    async def set(self, key: str, entry: CachedEntry) -> None:
        try:
            await self._redis.set(
                f"{self._prefix}:{key}", json.dumps(asdict(entry)), px=self._ttl_ms
            )
        except RedisError:
            _redis_down("set")

    async def try_lock(self, key: str) -> str | None:
        token = uuid.uuid4().hex
        try:
            acquired = await self._redis.set(
                f"{self._prefix}:{key}:lock", token, nx=True, px=self._lock_ttl_ms
            )
        except RedisError:
            _redis_down("lock")
            return token  # no coordination possible: just call upstream ourselves
        return token if acquired else None

    async def unlock(self, key: str, token: str) -> None:
        # Compare-and-delete: if our lock expired and someone else now leads, leave theirs alone.
        try:
            await self._unlock(keys=[f"{self._prefix}:{key}:lock"], args=[token])
        except RedisError:
            _redis_down("unlock")


def _redis_down(operation: str) -> None:
    REDIS_ERRORS.labels(component="cache").inc()
    log.warning("redis unavailable for cache %s; skipping cache", operation)


@dataclass(frozen=True)
class Lookup:
    entry: CachedEntry | None  # a hit
    lock_token: str | None  # we're the leader: call upstream, then store and unlock
    waited: bool  # we waited on another request's in-flight call


async def lookup_or_lead(
    cache: ResponseCache,
    key: str,
    wait_timeout_s: float,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Lookup:
    """Hit, or become the leader, or (after waiting too long) fall through with neither."""
    deadline = clock() + wait_timeout_s
    delay, waited = 0.02, False
    while True:
        if (entry := await cache.get(key)) is not None:
            return Lookup(entry, None, waited)
        if (token := await cache.try_lock(key)) is not None:
            return Lookup(None, token, waited)
        if clock() >= deadline:
            return Lookup(None, None, waited)
        waited = True
        await sleep(delay)
        delay = min(delay * 2, 0.2)


def replay_chunks(response: ChatResponse) -> list[ChatChunk]:
    """Turn a cached (non-streamed) response into an equivalent OpenAI chunk sequence."""

    def chunk(index: int, delta: Delta, finish: str | None = None) -> ChatChunk:
        choice = ChunkChoice(index=index, delta=delta, finish_reason=finish)
        return ChatChunk(
            id=response.id, created=response.created, model=response.model, choices=[choice]
        )

    chunks: list[ChatChunk] = []
    for choice in response.choices:
        message = choice.message
        chunks.append(chunk(choice.index, Delta(role="assistant", content="")))
        if message.content:
            chunks.append(chunk(choice.index, Delta(content=message.content)))
        if message.tool_calls:
            deltas = [
                ToolCallDelta(
                    index=i,
                    id=call.id,
                    type="function",
                    function=FunctionCallDelta(
                        name=call.function.name, arguments=call.function.arguments
                    ),
                )
                for i, call in enumerate(message.tool_calls)
            ]
            chunks.append(chunk(choice.index, Delta(tool_calls=deltas)))
        chunks.append(chunk(choice.index, Delta(), choice.finish_reason or "stop"))
    return chunks
