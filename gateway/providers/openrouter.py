import httpx

from gateway.providers.openai_compat import OpenAICompatibleProvider


class OpenRouterProvider(OpenAICompatibleProvider):
    """OpenRouter speaks the OpenAI wire format. Its SSE keep-alive comments are skipped by
    `iter_sse`, and mid-stream `{"error": ...}` events are handled by the base class."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        super().__init__("openrouter", client, base_url, api_key)
        # Optional attribution headers (https://openrouter.ai/docs/api-reference/overview).
        self._headers["X-Title"] = "Switchyard"
