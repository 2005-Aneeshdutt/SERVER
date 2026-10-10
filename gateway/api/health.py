import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from gateway.api.deps import ContainerDep

router = APIRouter()

_CHECK_TIMEOUT_S = 1.0


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never depends on Redis/Postgres (restarting won't fix them)."""
    return {"status": "ok"}


@router.get("/readyz")
async def readyz(request: Request, container: ContainerDep) -> JSONResponse:
    """Readiness for the load balancer: should *this replica* receive traffic?

    Only replica-local conditions make it 503 (currently: shutting down / draining).
    Shared dependencies (Redis, Postgres) are reported as `degraded` but never fail readiness:
    every replica shares them, so failing readiness on them would make nginx eject *all*
    replicas at once and turn a partial outage into a total one. See docs/decisions/0007.
    """
    names = list(container.readiness)
    results = await asyncio.gather(
        *(asyncio.wait_for(container.readiness[n](), _CHECK_TIMEOUT_S) for n in names),
        return_exceptions=True,
    )
    checks = {
        n: "ok" if r is None else f"error: {type(r).__name__}"
        for n, r in zip(names, results, strict=True)
    }
    draining = bool(getattr(request.app.state, "draining", False))
    body = {
        "ready": not draining,
        "draining": draining,
        "degraded": any(v != "ok" for v in checks.values()),
        "checks": checks,
    }
    return JSONResponse(body, status_code=503 if draining else 200)


@router.get("/metrics")
async def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
