"""Load shedding: cap in-flight requests per replica and reject the excess with 503 immediately.

Past saturation, a replica that accepts everything just queues: every request still gets served,
but seconds late, after most clients have given up. Rejecting the excess up front keeps latency
bounded for the requests we do accept and tells clients to retry elsewhere. See docs/decisions/0014.

Pure ASGI (not BaseHTTPMiddleware): it adds no task or copy per request, and the slot is held
until the response body is fully sent, so a long stream occupies its slot for its whole life.
"""

import json

from starlette.types import ASGIApp, Receive, Scope, Send

from gateway.core.errors import Overloaded
from gateway.observability.metrics import IN_FLIGHT, REQUESTS_SHED

# Never shed these: a load balancer that sees health checks fail would eject a busy replica and
# push its load onto the others, and losing /metrics would hide the overload being shed.
EXEMPT_PATHS = frozenset({"/healthz", "/readyz", "/metrics"})


class LoadShedder:
    """ASGI middleware. `limit` <= 0 disables shedding; it's set once at startup."""

    def __init__(self, app: ASGIApp, limit: int = 0) -> None:
        self.app = app
        self.limit = limit
        self.in_flight = 0  # one event loop per process, so a plain int is race-free

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in EXEMPT_PATHS:
            await self.app(scope, receive, send)
            return
        if 0 < self.limit <= self.in_flight:
            REQUESTS_SHED.inc()
            await _reject(send)
            return
        self.in_flight += 1
        IN_FLIGHT.set(self.in_flight)
        try:
            await self.app(scope, receive, send)
        finally:
            self.in_flight -= 1
            IN_FLIGHT.set(self.in_flight)


async def _reject(send: Send) -> None:
    error = Overloaded("Gateway replica is at capacity; retry shortly")
    body = json.dumps(error.to_body()).encode()
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode()),
        *((k.lower().encode(), v.encode()) for k, v in error.headers().items()),
    ]
    await send({"type": "http.response.start", "status": error.status_code, "headers": headers})
    await send({"type": "http.response.body", "body": body})
