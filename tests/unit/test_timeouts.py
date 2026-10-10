import asyncio
from collections.abc import AsyncIterator

import pytest

from gateway.core.errors import ProviderTimeout
from gateway.core.schemas import ChatChunk, ChunkChoice, Delta
from gateway.providers.timeouts import with_stream_timeouts, with_total_timeout


def chunk(content: str | None, role: str | None = None) -> ChatChunk:
    return ChatChunk(
        id="c", created=0, model="m", choices=[ChunkChoice(delta=Delta(role=role, content=content))]
    )


class Source:
    def __init__(self, items: list[tuple[float, ChatChunk]]) -> None:
        self.items = items
        self.closed = False

    async def gen(self) -> AsyncIterator[ChatChunk]:
        try:
            for delay, item in self.items:
                await asyncio.sleep(delay)
                yield item
        finally:
            self.closed = True


async def drain(it: AsyncIterator[ChatChunk]) -> list[ChatChunk]:
    return [c async for c in it]


async def test_passes_chunks_through() -> None:
    src = Source([(0, chunk("a")), (0, chunk("b"))])
    out = await drain(with_stream_timeouts(src.gen(), "p", 1, 1))
    assert [c.choices[0].delta.content for c in out] == ["a", "b"]


async def test_first_token_timeout() -> None:
    src = Source([(0.5, chunk("late"))])
    with pytest.raises(ProviderTimeout, match="first-token"):
        await drain(with_stream_timeouts(src.gen(), "p", first_token_s=0.05, total_s=5))
    assert src.closed


async def test_role_only_chunk_does_not_satisfy_first_token() -> None:
    src = Source([(0, chunk("", role="assistant")), (0.5, chunk("late"))])
    with pytest.raises(ProviderTimeout, match="first-token"):
        await drain(with_stream_timeouts(src.gen(), "p", first_token_s=0.1, total_s=5))


async def test_total_timeout_after_first_token() -> None:
    src = Source([(0, chunk("a")), (0.05, chunk("b")), (0.05, chunk("c")), (0.5, chunk("d"))])
    with pytest.raises(ProviderTimeout, match="total"):
        await drain(with_stream_timeouts(src.gen(), "p", first_token_s=0.1, total_s=0.3))


async def test_consumer_close_closes_source() -> None:
    src = Source([(0, chunk("a")), (0, chunk("b"))])
    stream = with_stream_timeouts(src.gen(), "p", 1, 1)
    await anext(stream)
    await stream.aclose()
    assert src.closed


async def test_total_timeout_for_non_streaming() -> None:
    with pytest.raises(ProviderTimeout):
        await with_total_timeout(asyncio.sleep(1), "p", 0.05)
