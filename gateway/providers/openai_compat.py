"""Adapter for providers that speak the OpenAI Chat Completions wire format natively."""

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import ValidationError

from gateway.core.errors import ProviderUnavailable
from gateway.core.schemas import ChatChunk, ChatRequest, ChatResponse
from gateway.providers.http import iter_sse, open_stream, parse_json_object, post_json
from gateway.routing.registry import ModelSpec


class OpenAICompatibleProvider:
    def __init__(
        self, name: str, client: httpx.AsyncClient, base_url: str, api_key: str | None
    ) -> None:
        self.name = name
        self._client = client
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    async def complete(self, req: ChatRequest, model: ModelSpec) -> ChatResponse:
        data = await post_json(
            self._client, self.name, self._url, self._headers, self._payload(req, model, False)
        )
        try:
            return ChatResponse.model_validate(data)
        except ValidationError as exc:
            raise ProviderUnavailable(self.name, "malformed completion from upstream") from exc

    async def stream(self, req: ChatRequest, model: ModelSpec) -> AsyncIterator[ChatChunk]:
        payload = self._payload(req, model, True)
        async with open_stream(self._client, self.name, self._url, self._headers, payload) as resp:
            async for _event, data in iter_sse(resp):
                if data.strip() == "[DONE]":
                    return
                yield self._parse_chunk(parse_json_object(self.name, data))

    def _payload(self, req: ChatRequest, model: ModelSpec, stream: bool) -> dict[str, Any]:
        body = req.model_dump(exclude_none=True, exclude={"model", "stream", "stream_options"})
        body["model"] = model.upstream_model
        if stream:
            body["stream"] = True
            # Always ask for usage upstream; the gateway decides whether the client sees it.
            body["stream_options"] = {"include_usage": True}
        return body

    def _parse_chunk(self, data: dict[str, Any]) -> ChatChunk:
        if "error" in data:
            raise ProviderUnavailable(self.name, f"error mid-stream: {json.dumps(data['error'])}")
        try:
            return ChatChunk.model_validate(data)
        except ValidationError as exc:
            raise ProviderUnavailable(self.name, "malformed stream chunk from upstream") from exc
