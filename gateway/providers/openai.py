import httpx

from gateway.providers.openai_compat import OpenAICompatibleProvider


class OpenAIProvider(OpenAICompatibleProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, api_key: str) -> None:
        super().__init__("openai", client, base_url, api_key)
