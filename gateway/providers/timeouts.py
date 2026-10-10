"""Upstream timeouts, applied uniformly around any provider.

Three separate budgets:
- connect: set on the shared httpx client (fails fast when a provider is unreachable),
- first token: max wait until the stream yields real content (catches stuck queues),
- total: hard ceiling on the whole call (catches slow, never-ending generations).
"""

import asyncio
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable

from gateway.core.errors import ProviderTimeout
from gateway.core.schemas import ChatChunk


async def with_total_timeout[T](call: Awaitable[T], provider: str, total_s: float) -> T:
    try:
        return await asyncio.wait_for(call, timeout=total_s)
    except TimeoutError:
        raise ProviderTimeout(provider, f"no response within {total_s:g}s") from None


async def with_stream_timeouts(
    source: AsyncIterator[ChatChunk], provider: str, first_token_s: float, total_s: float
) -> AsyncGenerator[ChatChunk, None]:
    loop = asyncio.get_running_loop()
    started = loop.time()
    total_deadline = started + total_s
    first_deadline = min(started + first_token_s, total_deadline)
    seen_content = False
    try:
        while True:
            deadline = total_deadline if seen_content else first_deadline
            try:
                chunk = await asyncio.wait_for(anext(source), timeout=deadline - loop.time())
            except StopAsyncIteration:
                return
            except TimeoutError:
                which = "total" if seen_content else "first-token"
                raise ProviderTimeout(provider, f"{which} timeout exceeded") from None
            seen_content = seen_content or chunk.has_content()
            yield chunk
    finally:
        aclose = getattr(source, "aclose", None)
        if aclose is not None:
            await aclose()
