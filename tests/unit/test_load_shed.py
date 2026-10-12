import asyncio
from collections.abc import Coroutine
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.types import Message, Receive, Scope, Send

from gateway.api.load_shed import LoadShedder
from gateway.observability.metrics import REQUESTS_SHED


def shed_total() -> float:
    return float(REQUESTS_SHED._value.get())


class SlowApp:
    """An app whose /work requests block until released, so a test controls concurrency."""

    def __init__(self, limit: int) -> None:
        self.release = asyncio.Event()
        self.started = 0
        app = FastAPI()

        @app.post("/work")
        async def work() -> dict[str, str]:
            self.started += 1
            await self.release.wait()
            return {"ok": "yes"}

        @app.get("/healthz")
        async def healthz() -> dict[str, str]:
            return {"status": "ok"}

        app.add_middleware(LoadShedder, limit=limit)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gw"
        )

    async def wait_started(self, n: int) -> None:
        for _ in range(200):
            if self.started >= n:
                return
            await asyncio.sleep(0.005)
        raise AssertionError(f"only {self.started} of {n} requests started")


async def test_requests_past_the_cap_are_shed_with_503_and_retry_after() -> None:
    gw = SlowApp(limit=3)
    held = [asyncio.create_task(gw.client.post("/work")) for _ in range(3)]
    await gw.wait_started(3)
    before = shed_total()

    rejected = await gw.client.post("/work")

    assert rejected.status_code == 503
    assert rejected.headers["retry-after"] == "1"
    assert rejected.json()["error"]["code"] == "overloaded"
    assert shed_total() - before == 1
    gw.release.set()
    assert [r.status_code for r in await asyncio.gather(*held)] == [200, 200, 200]


async def test_slots_are_released_after_responses_finish() -> None:
    gw = SlowApp(limit=2)
    gw.release.set()
    for _ in range(10):  # sequential: never more than one in flight
        assert (await gw.client.post("/work")).status_code == 200


async def test_health_checks_are_never_shed() -> None:
    gw = SlowApp(limit=1)
    held = asyncio.create_task(gw.client.post("/work"))
    await gw.wait_started(1)
    assert (await gw.client.get("/healthz")).status_code == 200
    gw.release.set()
    await held


async def test_a_stream_holds_its_slot_until_the_body_ends() -> None:
    # Raw ASGI: httpx's ASGITransport buffers whole bodies, so it can't observe a stream mid-way.
    release = asyncio.Event()
    sent: list[Message] = []
    first_chunk = asyncio.Event()

    async def streaming_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        first_chunk.set()  # headers + first chunk are out, body still open
        await release.wait()
        await send({"type": "http.response.body", "body": b"last"})

    shedder = LoadShedder(streaming_app, limit=1)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    async def record(message: Message) -> None:
        sent.append(message)

    def call() -> Coroutine[Any, Any, None]:
        return shedder({"type": "http", "path": "/v1/chat/completions"}, receive, record)

    stream = asyncio.create_task(call())
    await first_chunk.wait()
    await call()
    assert sent[2]["status"] == 503
    release.set()
    await stream
    assert shedder.in_flight == 0


async def test_zero_limit_disables_shedding() -> None:
    gw = SlowApp(limit=0)
    held = [asyncio.create_task(gw.client.post("/work")) for _ in range(50)]
    await gw.wait_started(50)
    gw.release.set()
    assert {r.status_code for r in await asyncio.gather(*held)} == {200}
