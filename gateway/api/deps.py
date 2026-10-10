from typing import Annotated, cast

from fastapi import Depends, Header, Request

from gateway.container import Container
from gateway.core.auth import KEY_PREFIX, ApiKeyRecord, bearer_token, hash_key
from gateway.core.errors import InvalidAPIKey


def get_container(request: Request) -> Container:
    return cast(Container, request.app.state.container)


ContainerDep = Annotated[Container, Depends(get_container)]


async def authenticate(
    container: ContainerDep, authorization: Annotated[str | None, Header()] = None
) -> ApiKeyRecord:
    raw = bearer_token(authorization)
    if raw is None or not raw.startswith(KEY_PREFIX):
        raise InvalidAPIKey("Missing or malformed API key. Send 'Authorization: Bearer gw-...'.")
    record = await container.keys.get_by_hash(hash_key(raw))
    if record is None or not record.active:
        raise InvalidAPIKey("Invalid or revoked API key.")
    return record


KeyDep = Annotated[ApiKeyRecord, Depends(authenticate)]
