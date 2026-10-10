from typing import Any

import httpx

from gateway.core.schemas import ChatChunk, Usage
from gateway.providers.openai_compat import OpenAICompatibleProvider


class GroqProvider(OpenAICompatibleProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        super().__init__("groq", client, base_url, api_key)

    def _parse_chunk(self, data: dict[str, Any]) -> ChatChunk:
        # Groq has historically reported stream usage under `x_groq.usage` on the last chunk
        # instead of (or as well as) the standard `usage` field. Normalize and strip it.
        x_groq = data.pop("x_groq", None)
        chunk = super()._parse_chunk(data)
        if chunk.usage is None and isinstance(x_groq, dict) and "usage" in x_groq:
            chunk.usage = Usage.model_validate(x_groq["usage"])
        return chunk
