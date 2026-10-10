import httpx

from gateway.providers.openai_compat import OpenAICompatibleProvider


class OllamaProvider(OpenAICompatibleProvider):
    """Local Ollama via its OpenAI-compatible `/v1` endpoint. No API key needed."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        super().__init__("ollama", client, base_url, None)
