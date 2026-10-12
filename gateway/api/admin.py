"""Admin API: key management and usage. Authenticated with ADMIN_TOKEN (Bearer)."""

import hmac
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field

from gateway.api.deps import ContainerDep
from gateway.core.auth import CachedKeyStore, bearer_token
from gateway.core.errors import AdminDisabled, AdminUnauthorized, InvalidRequest, NotFound
from gateway.db.models import ApiKeyRow
from gateway.db.repositories import SqlKeyStore, SqlUsageReader

_PLACEHOLDER_TOKENS = {"", "change-me"}


async def require_admin(
    container: ContainerDep, authorization: Annotated[str | None, Header()] = None
) -> None:
    configured = container.settings.admin_token.get_secret_value()
    if configured in _PLACEHOLDER_TOKENS:
        raise AdminDisabled("Admin API is disabled until ADMIN_TOKEN is set to a real secret.")
    token = bearer_token(authorization)
    # Constant-time comparison: don't leak how many leading characters matched.
    if token is None or not hmac.compare_digest(token.encode(), configured.encode()):
        raise AdminUnauthorized("Invalid admin token.")


router = APIRouter(prefix="/admin", dependencies=[Depends(require_admin)])


RoutingQuality = Literal["low", "balanced", "high"]


class KeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    monthly_spend_cap_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    allowed_models: list[str] | None = None
    routing_quality: RoutingQuality = "balanced"


class KeyUpdate(BaseModel):
    """Only fields present in the body change; send null to clear a limit."""

    name: str | None = Field(default=None, min_length=1, max_length=200)
    rpm_limit: int | None = Field(default=None, ge=1)
    tpm_limit: int | None = Field(default=None, ge=1)
    monthly_spend_cap_usd: Decimal | None = Field(default=None, ge=0, decimal_places=6)
    allowed_models: list[str] | None = None
    active: bool | None = None
    routing_quality: RoutingQuality | None = None


class KeyOut(BaseModel):
    id: uuid.UUID
    name: str
    key_prefix: str
    rpm_limit: int | None
    tpm_limit: int | None
    monthly_spend_cap_usd: Decimal | None
    allowed_models: list[str] | None
    active: bool
    routing_quality: str
    created_at: datetime | None

    @classmethod
    def of(cls, row: ApiKeyRow) -> "KeyOut":
        return cls(
            id=row.id,
            name=row.name,
            key_prefix=row.key_prefix,
            rpm_limit=row.rpm_limit,
            tpm_limit=row.tpm_limit,
            monthly_spend_cap_usd=row.monthly_spend_cap_usd,
            allowed_models=row.allowed_models,
            active=row.active,
            routing_quality=row.routing_quality,
            created_at=row.created_at,
        )


class KeyCreated(KeyOut):
    key: str = Field(description="The raw API key. Shown once; only its hash is stored.")


def _store(container: ContainerDep) -> SqlKeyStore:
    if container.admin_keys is None:
        raise AdminDisabled("Admin API needs a database.")
    return container.admin_keys


def _usage_reader(container: ContainerDep) -> SqlUsageReader:
    if container.usage_reader is None:
        raise AdminDisabled("Admin API needs a database.")
    return container.usage_reader


StoreDep = Annotated[SqlKeyStore, Depends(_store)]


def _check_aliases(container: ContainerDep, aliases: list[str] | None) -> None:
    known = {spec.alias for spec in container.registry.all()} | {"auto"}
    unknown = sorted(set(aliases or []) - known)
    if unknown:
        raise InvalidRequest(f"Unknown model aliases: {', '.join(unknown)}")


@router.post("/keys", status_code=201)
async def create_key(body: KeyCreate, container: ContainerDep, store: StoreDep) -> KeyCreated:
    _check_aliases(container, body.allowed_models)
    raw, record = await store.create(
        body.name,
        rpm_limit=body.rpm_limit,
        tpm_limit=body.tpm_limit,
        monthly_spend_cap_usd=body.monthly_spend_cap_usd,
        allowed_models=body.allowed_models,
        routing_quality=body.routing_quality,
    )
    row = await store.get_row(record.id)
    assert row is not None
    return KeyCreated(**KeyOut.of(row).model_dump(), key=raw)


@router.get("/keys")
async def list_keys(store: StoreDep) -> list[KeyOut]:
    return [KeyOut.of(row) for row in await store.list_rows()]


@router.patch("/keys/{key_id}")
async def update_key(
    key_id: uuid.UUID, body: KeyUpdate, container: ContainerDep, store: StoreDep
) -> KeyOut:
    changes = body.model_dump(include=body.model_fields_set)
    if changes.get("name") is None:
        changes.pop("name", None)
    for non_nullable in ("active", "routing_quality"):
        if changes.get(non_nullable) is None:
            changes.pop(non_nullable, None)
    _check_aliases(container, changes.get("allowed_models"))
    row = await store.update(key_id, changes)
    if row is None:
        raise NotFound(f"No API key with id {key_id}")
    if isinstance(container.keys, CachedKeyStore):
        container.keys.evict(key_id)  # this replica sees the change now; others within the TTL
    return KeyOut.of(row)


@router.get("/usage")
async def usage(
    container: ContainerDep,
    since: datetime | None = None,
    until: datetime | None = None,
    key_id: Annotated[uuid.UUID | None, Query()] = None,
) -> dict[str, Any]:
    """Cost, tokens, latency and cache hits per (key, alias). Defaults to the last 30 days."""
    end = until or datetime.now(UTC)
    start = since or end - timedelta(days=30)
    raw_rows, source, lag_s = await _usage_reader(container).usage(start, end, key_id)
    rows = [_format_row(r) for r in raw_rows]
    requests = sum(int(r["requests"]) for r in rows)
    hits = sum(int(r["cache_hits"]) for r in rows)
    total_cost = sum((Decimal(r["cost_usd"]) for r in rows), Decimal(0))
    return {
        "since": start,
        "until": end,
        "source": source,  # "replica" normally; "primary" if the replica is down
        "replica_lag_s": lag_s,
        "totals": {
            "requests": requests,
            "cost_usd": _usd(total_cost),
            "cache_hit_rate": round(hits / requests, 4) if requests else 0.0,
        },
        "rows": rows,
    }


def _usd(value: Decimal | None) -> str:
    """Fixed-point string: Decimal's default str() uses exponents for tiny values (9.9E-7)."""
    return format((value or Decimal(0)).quantize(Decimal("1e-10")).normalize(), "f")


def _format_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if "cost_usd" in out:
        out["cost_usd"] = _usd(row["cost_usd"])
    for name in ("avg_latency_ms", "p50_latency_ms", "p95_latency_ms", "avg_ttft_ms"):
        if out.get(name) is not None:
            out[name] = round(float(out[name]), 1)
    return out


# ---- providers: status and the kill switch ------------------------------------------------


class OutageRequest(BaseModel):
    down: bool


@router.get("/providers")
async def list_providers(container: ContainerDep) -> list[dict[str, Any]]:
    """Every provider in the registry, whether it's configured, and its circuit state."""
    by_provider: dict[str, list[str]] = {}
    for spec in container.registry.all():
        by_provider.setdefault(spec.provider, []).append(spec.alias)
    out = []
    for name, aliases in sorted(by_provider.items()):
        configured = name in container.providers
        state = await container.breaker.state(name) if configured else "not_configured"
        out.append({"name": name, "configured": configured, "state": state, "aliases": aliases})
    return out


@router.post("/providers/{name}/outage")
async def set_outage(name: str, body: OutageRequest, container: ContainerDep) -> dict[str, Any]:
    """Take a provider down for ALL traffic (or bring it back). Requests fail over to the next
    alias in their chain, exactly as in a real outage."""
    if name not in container.providers:
        raise NotFound(f"No configured provider named {name!r}")
    await container.breaker.set_forced_down(name, body.down)
    return {"name": name, "state": await container.breaker.state(name)}


# ---- dashboard feeds ------------------------------------------------------------------------


@router.get("/usage/timeseries")
async def usage_timeseries(
    container: ContainerDep,
    hours: Annotated[int, Query(ge=1, le=168)] = 24,
    bucket: Literal["minute", "hour"] = "hour",
) -> dict[str, Any]:
    end = datetime.now(UTC)
    rows, source, lag_s = await _usage_reader(container).timeseries(
        end - timedelta(hours=hours), end, bucket
    )
    return {
        "bucket": bucket,
        "source": source,
        "replica_lag_s": lag_s,
        "rows": [_format_row(r) for r in rows],
    }


@router.get("/requests")
async def recent_requests(
    container: ContainerDep, limit: Annotated[int, Query(ge=1, le=200)] = 50
) -> dict[str, Any]:
    rows, source, lag_s = await _usage_reader(container).recent(limit)
    return {"source": source, "replica_lag_s": lag_s, "rows": [_format_row(r) for r in rows]}
