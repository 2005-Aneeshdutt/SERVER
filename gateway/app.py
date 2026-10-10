"""App factory. Run with: uvicorn gateway.app:create_app --factory"""

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from gateway.api import admin, chat, health, models
from gateway.container import Container, build_container
from gateway.core.config import get_settings
from gateway.core.errors import GatewayError

log = logging.getLogger(__name__)

ContainerFactory = Callable[[], Awaitable[Container]]


async def _default_container() -> Container:
    return await build_container(get_settings())


def create_app(container_factory: ContainerFactory | None = None) -> FastAPI:
    factory = container_factory or _default_container

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = await factory()
        app.state.container = container
        await container.request_log.start()
        app.state.draining = False
        try:
            yield
        finally:
            app.state.draining = True  # /readyz -> 503 so the load balancer stops sending traffic
            await container.request_log.stop()
            for close in container.closers:
                await close()

    app = FastAPI(title="LLM Gateway", version="0.1.0", lifespan=lifespan)
    app.include_router(chat.router)
    app.include_router(models.router)
    app.include_router(health.router)
    app.include_router(admin.router)
    _install_error_handlers(app)
    return app


def _install_error_handlers(app: FastAPI) -> None:
    async def gateway_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, GatewayError)
        return JSONResponse(exc.to_body(), status_code=exc.status_code, headers=exc.headers())

    async def validation_error(_: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, RequestValidationError)
        first = exc.errors()[0] if exc.errors() else {}
        param = ".".join(str(p) for p in first.get("loc", ()) if p != "body") or None
        body = {
            "error": {
                "message": f"{param}: {first.get('msg', 'invalid request')}",
                "type": "invalid_request_error",
                "code": "invalid_request",
                "param": param,
            }
        }
        return JSONResponse(body, status_code=400)

    async def unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        body = {
            "error": {
                "message": "Internal gateway error",
                "type": "api_error",
                "code": "internal_error",
                "param": None,
            }
        }
        return JSONResponse(body, status_code=500)

    app.add_exception_handler(GatewayError, gateway_error)
    app.add_exception_handler(RequestValidationError, validation_error)
    app.add_exception_handler(Exception, unexpected_error)
