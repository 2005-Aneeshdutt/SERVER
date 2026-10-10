import json

import httpx
import respx

from gateway.routing.smart import RouterModel, SmartRouter
from tests.conftest import Gateway
from tests.helpers import KeywordEmbedder, openai_completion

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MODEL = RouterModel(
    cheap_alias="or-llama-8b",
    strong_alias="or-llama-70b",
    weights=(-2.0, 2.0),
    bias=0.0,
    thresholds={"low": 0.05, "balanced": 0.5, "high": 0.95},
)


def enable_router(gateway: Gateway) -> None:
    c = gateway.container
    c.router = SmartRouter(MODEL, KeywordEmbedder(), c.registry, c.breaker, default_max_tokens=64)


def ask(content: str) -> dict[str, object]:
    return {"model": "auto", "messages": [{"role": "user", "content": content}]}


async def test_auto_routes_easy_prompt_to_cheap_model(
    gateway: Gateway, upstream: respx.Router
) -> None:
    enable_router(gateway)
    route = upstream.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("Lima"))
    )
    response = await gateway.client.post("/v1/chat/completions", json=ask("Capital of Peru?"))

    assert response.status_code == 200
    assert (
        json.loads(route.calls.last.request.content)["model"] == "meta-llama/llama-3.1-8b-instruct"
    )
    assert response.headers["x-gateway-route"] == "cheap"
    assert response.headers["x-gateway-route-reason"] == "classifier"
    assert float(response.headers["x-gateway-route-probability"]) > 0.5
    assert response.headers["x-gateway-alias"] == "or-llama-8b"
    [record] = await gateway.logged()
    assert (record.alias, record.route) == ("auto", "cheap")
    assert record.route_probability is not None and record.route_probability > 0.5


async def test_auto_routes_hard_prompt_to_strong_model(
    gateway: Gateway, upstream: respx.Router
) -> None:
    enable_router(gateway)
    route = upstream.post(OPENROUTER_URL).mock(
        return_value=httpx.Response(200, json=openai_completion("QED"))
    )
    response = await gateway.client.post(
        "/v1/chat/completions", json=ask("Prove the theorem step by step")
    )
    assert response.headers["x-gateway-route"] == "strong"
    assert (
        json.loads(route.calls.last.request.content)["model"] == "meta-llama/llama-3.3-70b-instruct"
    )


async def test_auto_is_rejected_when_router_is_not_enabled(gateway: Gateway) -> None:
    response = await gateway.client.post("/v1/chat/completions", json=ask("hi"))
    assert response.status_code == 400
    assert "auto" in response.json()["error"]["message"]


async def test_models_endpoint_lists_auto_only_when_enabled(gateway: Gateway) -> None:
    ids = [m["id"] for m in (await gateway.client.get("/v1/models")).json()["data"]]
    assert "auto" not in ids
    enable_router(gateway)
    ids = [m["id"] for m in (await gateway.client.get("/v1/models")).json()["data"]]
    assert ids[0] == "auto"
