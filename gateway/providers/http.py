"""HTTP plumbing shared by adapters: error normalization, JSON POST, SSE streaming."""

import json
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

import httpx

from gateway.core.errors import (
    AuthError,
    BadRequest,
    ProviderError,
    ProviderTimeout,
    ProviderUnavailable,
    RateLimited,
)

_MAX_ERROR_TEXT = 500


def _upstream_message(body: str) -> str:
    try:
        data = json.loads(body)
    except ValueError:
        return body[:_MAX_ERROR_TEXT] or "no response body"
    if isinstance(data, list) and data:  # Gemini sometimes wraps errors in a list
        data = data[0]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return str(error["message"])[:_MAX_ERROR_TEXT]
    return body[:_MAX_ERROR_TEXT]


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None  # HTTP-date form; not worth parsing


def map_status_error(
    provider: str, status: int, body: str, headers: Mapping[str, str]
) -> ProviderError:
    message = _upstream_message(body)
    if status == 429:
        return RateLimited(provider, message, status, _retry_after(headers))
    if status in (401, 403) or (status == 400 and "API_KEY_INVALID" in body):
        return AuthError(provider, message, status)
    if status == 408:
        return ProviderTimeout(provider, message, status)
    if status == 402:
        # Our upstream account is out of credits (OpenRouter): the gateway's problem, fail over.
        return ProviderUnavailable(provider, message, status)
    if status == 404:
        # Unknown upstream model id = our registry is wrong, not the client's request.
        return ProviderUnavailable(provider, message, status)
    if status >= 500:
        return ProviderUnavailable(provider, message, status)
    return BadRequest(provider, message, status)


def map_transport_error(provider: str, exc: httpx.HTTPError) -> ProviderError:
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeout(provider, f"{type(exc).__name__}: {exc}")
    return ProviderUnavailable(provider, f"{type(exc).__name__}: {exc}")


async def post_json(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        response = await client.post(url, headers=dict(headers), json=payload)
    except httpx.HTTPError as exc:
        raise map_transport_error(provider, exc) from exc
    if response.status_code >= 400:
        raise map_status_error(provider, response.status_code, response.text, response.headers)
    return parse_json_object(provider, response.text)


def parse_json_object(provider: str, text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ProviderUnavailable(provider, "malformed JSON from upstream") from exc
    if not isinstance(data, dict):
        raise ProviderUnavailable(provider, "unexpected JSON shape from upstream")
    return data


@asynccontextmanager
async def open_stream(
    client: httpx.AsyncClient,
    provider: str,
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
) -> AsyncIterator[httpx.Response]:
    """Open a streaming POST. Non-2xx and transport errors (including mid-stream ones raised
    inside the `async with` body) come out as normalized ProviderErrors."""
    try:
        async with client.stream("POST", url, headers=dict(headers), json=payload) as response:
            if response.status_code >= 400:
                body = (await response.aread()).decode("utf-8", errors="replace")
                raise map_status_error(provider, response.status_code, body, response.headers)
            yield response
    except httpx.HTTPError as exc:
        raise map_transport_error(provider, exc) from exc


async def iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str | None, str]]:
    """Yield (event, data) pairs from a text/event-stream body."""
    event: str | None = None
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if not line:
            if data_lines:
                yield event, "\n".join(data_lines)
            event, data_lines = None, []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event, "\n".join(data_lines)
