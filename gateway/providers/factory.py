"""Build provider adapters from settings. Providers without credentials are simply absent."""

import httpx

from gateway.core.config import Settings, secret_or_none
from gateway.providers.anthropic import AnthropicProvider
from gateway.providers.base import Provider
from gateway.providers.gemini import GeminiProvider
from gateway.providers.groq import GroqProvider
from gateway.providers.ollama import OllamaProvider
from gateway.providers.openai import OpenAIProvider
from gateway.providers.openai_compat import OpenAICompatibleProvider
from gateway.providers.openrouter import OpenRouterProvider


def make_http_client(settings: Settings) -> httpx.AsyncClient:
    timeout = httpx.Timeout(settings.total_timeout_s, connect=settings.connect_timeout_s)
    limits = httpx.Limits(max_connections=500, max_keepalive_connections=100)
    return httpx.AsyncClient(timeout=timeout, limits=limits)


def build_providers(settings: Settings, client: httpx.AsyncClient) -> dict[str, Provider]:
    providers: dict[str, Provider] = {}
    if key := secret_or_none(settings.groq_api_key):
        providers["groq"] = GroqProvider(client, settings.groq_base_url, key)
    if key := secret_or_none(settings.gemini_api_key):
        providers["gemini"] = GeminiProvider(client, settings.gemini_base_url, key)
    if key := secret_or_none(settings.openai_api_key):
        providers["openai"] = OpenAIProvider(client, settings.openai_base_url, key)
    if key := secret_or_none(settings.anthropic_api_key):
        providers["anthropic"] = AnthropicProvider(
            client, settings.anthropic_base_url, key, settings.default_max_tokens
        )
    if key := secret_or_none(settings.openrouter_api_key):
        providers["openrouter"] = OpenRouterProvider(client, settings.openrouter_base_url, key)
    if settings.ollama_base_url:
        providers["ollama"] = OllamaProvider(client, settings.ollama_base_url)
    if settings.replay_base_url:
        providers["replay"] = OpenAICompatibleProvider(
            "replay", client, settings.replay_base_url, None
        )
    return providers
