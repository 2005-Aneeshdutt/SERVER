"""Admin API against the real Postgres (and Redis), through a fully built container."""

from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from gateway.app import create_app
from gateway.container import Container, build_container
from gateway.core.config import Settings
from tests.conftest import ROOT

pytestmark = pytest.mark.integration
ADMIN = {"Authorization": "Bearer test-admin-token"}


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(
        _env_file=None,
        models_file=str(ROOT / "models.yaml"),
        admin_token=SecretStr("test-admin-token"),
        # Not testing routing here: skip loading the embedding model (~20 s, CPU-heavy).
        router_model_path="does-not-exist.json",
        demo_enabled=True,
        demo_keys_per_hour=1000,
    )

    async def factory() -> Container:
        return await build_container(settings)

    app = create_app(factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://gw") as c:
            yield c


async def test_admin_requires_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/admin/keys")).status_code == 401
    bad = await client.get("/admin/keys", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401


async def test_key_lifecycle(client: httpx.AsyncClient) -> None:
    created = await client.post(
        "/admin/keys",
        headers=ADMIN,
        json={
            "name": "it-admin",
            "rpm_limit": 30,
            "monthly_spend_cap_usd": "2.5",
            "allowed_models": ["or-llama-8b"],
        },
    )
    assert created.status_code == 201
    body = created.json()
    raw, key_id = body["key"], body["id"]
    assert raw.startswith("gw-") and body["key_prefix"] == raw[:7]

    listed = (await client.get("/admin/keys", headers=ADMIN)).json()
    mine = next(k for k in listed if k["id"] == key_id)
    assert "key" not in mine  # the raw key is never shown again
    assert mine["monthly_spend_cap_usd"] == "2.500000"

    # The new key works immediately (misses are never cached).
    user = {"Authorization": f"Bearer {raw}"}
    assert [m["id"] for m in (await client.get("/v1/models", headers=user)).json()["data"]] == []

    patched = await client.patch(
        f"/admin/keys/{key_id}", headers=ADMIN, json={"active": False, "rpm_limit": None}
    )
    assert patched.status_code == 200
    assert patched.json()["active"] is False and patched.json()["rpm_limit"] is None
    # Revocation is immediate on the replica that processed it (cache evicted).
    assert (await client.get("/v1/models", headers=user)).status_code == 401


async def test_validation(client: httpx.AsyncClient) -> None:
    unknown = await client.post(
        "/admin/keys", headers=ADMIN, json={"name": "x", "allowed_models": ["gpt-9"]}
    )
    assert unknown.status_code == 400
    missing = await client.patch(
        "/admin/keys/00000000-0000-0000-0000-000000000000", headers=ADMIN, json={"active": False}
    )
    assert missing.status_code == 404


async def test_usage_aggregates(client: httpx.AsyncClient) -> None:
    response = await client.get("/admin/usage", headers=ADMIN)
    assert response.status_code == 200
    body = response.json()
    assert set(body["totals"]) == {"requests", "cost_usd", "cache_hit_rate"}
    for row in body["rows"]:
        assert {"key_id", "alias", "requests", "cost_usd", "p95_latency_ms", "cache_hits"} <= set(
            row
        )


async def test_dashboard_feeds(client: httpx.AsyncClient) -> None:
    series = await client.get("/admin/usage/timeseries?hours=24&bucket=hour", headers=ADMIN)
    assert series.status_code == 200
    assert series.json()["bucket"] == "hour"
    for row in series.json()["rows"]:
        assert {"bucket", "requests", "errors", "cost_usd", "p95_latency_ms"} <= set(row)
    recent = await client.get("/admin/requests?limit=5", headers=ADMIN)
    assert recent.status_code == 200
    assert len(recent.json()["rows"]) <= 5
    providers = await client.get("/admin/providers", headers=ADMIN)
    assert {p["name"] for p in providers.json()} >= {"groq", "gemini", "openrouter"}


async def test_demo_key_is_capped_and_scoped(client: httpx.AsyncClient) -> None:
    created = await client.post("/demo/keys")
    assert created.status_code == 201
    body = created.json()
    assert body["key"].startswith("gw-") and body["name"].startswith("demo-")
    assert body["monthly_spend_cap_usd"] == "0.02" and body["rpm_limit"] == 10
    # Free-tier models only: a public demo lets anyone mint keys and the budget is $0.
    assert "auto" not in body["allowed_models"]
    assert all(not m.startswith("or-") for m in body["allowed_models"])
    listed = (await client.get("/admin/keys", headers=ADMIN)).json()
    mine = next(k for k in listed if k["name"] == body["name"])
    assert mine["monthly_spend_cap_usd"] == "0.020000"
