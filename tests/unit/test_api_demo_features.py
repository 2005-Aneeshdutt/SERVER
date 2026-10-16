"""Playground/dashboard support: simulated outages, provider kill switch, stream cost."""

import json
from typing import Any

import httpx
import respx

from tests.conftest import Gateway
from tests.helpers import GROQ_URL, openai_chunks, openai_completion, sse_response

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
BODY: dict[str, Any] = {
    "model": "groq-gpt-oss-20b",
    "messages": [{"role": "user", "content": "Hi"}],
}
ADMIN = {"Authorization": "Bearer test-admin-token"}


async def test_simulated_outage_fails_over_for_this_request_only(
    gateway: Gateway, upstream: respx.Router
) -> None:
    groq = upstream.post(GROQ_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    upstream.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=openai_completion()))

    simulated = await gateway.client.post(
        "/v1/chat/completions", json=BODY, headers={"X-Gateway-Simulate-Outage": "groq"}
    )
    assert simulated.status_code == 200
    assert simulated.headers["x-gateway-provider"] == "openrouter"
    assert simulated.headers["x-gateway-fallbacks"] == "1"
    assert not groq.called
    assert gateway.breaker.failures == []  # shared circuit state untouched

    normal = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert normal.headers["x-gateway-provider"] == "groq"  # other requests unaffected


async def test_simulating_every_provider_down_is_503(gateway: Gateway) -> None:
    response = await gateway.client.post(
        "/v1/chat/completions",
        json=BODY,
        headers={"X-Gateway-Simulate-Outage": "groq, openrouter, gemini"},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_provider_available"


async def test_stream_usage_chunk_reports_cost(gateway: Gateway, upstream: respx.Router) -> None:
    upstream.post(GROQ_URL).mock(return_value=sse_response(openai_chunks(["a", "b"])))
    body = {**BODY, "stream": True, "stream_options": {"include_usage": True}}
    response = await gateway.client.post("/v1/chat/completions", json=body)
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: {")
    ]
    final = events[-1]
    assert final["choices"] == []
    # 12 prompt * $0.075/M + 2 completion * $0.30/M
    assert final["x_gateway"]["cost_usd"] == "0.0000015000"


async def test_admin_lists_providers_and_takes_one_down(
    gateway: Gateway, upstream: respx.Router
) -> None:
    providers = (await gateway.client.get("/admin/providers", headers=ADMIN)).json()
    groq = next(p for p in providers if p["name"] == "groq")
    assert groq == {
        "name": "groq",
        "configured": True,
        "state": "closed",
        "aliases": ["groq-gpt-oss-20b", "groq-gpt-oss-120b"],
    }

    down = await gateway.client.post(
        "/admin/providers/groq/outage", json={"down": True}, headers=ADMIN
    )
    assert down.json() == {"name": "groq", "state": "open"}
    upstream.post(OPENROUTER_URL).mock(return_value=httpx.Response(200, json=openai_completion()))
    response = await gateway.client.post("/v1/chat/completions", json=BODY)
    assert response.headers["x-gateway-provider"] == "openrouter"  # everyone fails over

    await gateway.client.post("/admin/providers/groq/outage", json={"down": False}, headers=ADMIN)
    assert "groq" not in gateway.breaker.open


async def test_kill_switch_requires_admin_and_a_known_provider(gateway: Gateway) -> None:
    no_auth = await gateway.client.post("/admin/providers/groq/outage", json={"down": True})
    assert no_auth.status_code == 401
    unknown = await gateway.client.post(
        "/admin/providers/nope/outage", json={"down": True}, headers=ADMIN
    )
    assert unknown.status_code == 404


async def test_demo_keys_are_off_unless_enabled(gateway: Gateway) -> None:
    assert (await gateway.client.post("/demo/keys")).status_code == 404


def test_default_demo_models_are_free_tier_only() -> None:
    """A public demo lets anyone mint keys and the budget is $0: no paid provider, no `auto`
    (it routes to OpenRouter models)."""
    from gateway.core.config import Settings
    from gateway.routing.registry import ModelRegistry

    settings = Settings(_env_file=None)
    registry = ModelRegistry.from_yaml(settings.models_file)
    aliases = [a.strip() for a in settings.demo_models.split(",")]
    assert "auto" not in aliases
    assert {registry.get(a).provider for a in aliases} <= {"groq", "gemini"}
