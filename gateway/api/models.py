from typing import Any

from fastapi import APIRouter

from gateway.api.deps import ContainerDep, KeyDep

router = APIRouter()


@router.get("/v1/models")
async def list_models(container: ContainerDep, key: KeyDep) -> dict[str, Any]:
    """Aliases this key may use whose provider is configured."""
    data = [
        {"id": spec.alias, "object": "model", "created": 0, "owned_by": spec.provider}
        for spec in container.registry.all()
        if spec.provider in container.providers and key.may_use(spec.alias)
    ]
    if container.router is not None and key.may_use("auto"):
        data.insert(0, {"id": "auto", "object": "model", "created": 0, "owned_by": "gateway"})
    return {"object": "list", "data": data}
