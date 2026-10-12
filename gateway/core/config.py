"""Runtime settings, read from environment variables / `.env`."""

from decimal import Decimal
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://gateway:gateway@localhost:5432/gateway"
    # Optional streaming read replica for dashboard/usage queries (never for spend checks).
    database_read_url: str | None = None
    redis_url: str = "redis://localhost:6379/0"
    admin_token: SecretStr = SecretStr("change-me")
    models_file: str = "models.yaml"

    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None

    groq_base_url: str = "https://api.groq.com/openai/v1"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    openai_base_url: str = "https://api.openai.com/v1"
    anthropic_base_url: str = "https://api.anthropic.com/v1"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Ollama is optional: set e.g. http://localhost:11434/v1 to enable it.
    ollama_base_url: str | None = None

    connect_timeout_s: float = 5.0
    first_token_timeout_s: float = 20.0
    total_timeout_s: float = 120.0

    default_max_tokens: int = 1024

    retry_max_attempts_per_provider: int = 2
    retry_base_delay_s: float = 0.2
    retry_max_delay_s: float = 2.0

    breaker_failure_threshold: int = 5
    breaker_window_s: float = 30.0
    breaker_cooldown_s: float = 30.0
    breaker_probe_ttl_s: float = 15.0

    redis_timeout_s: float = 0.5

    # Spend limits: reservations of crashed requests are released after this long.
    spend_reservation_ttl_s: float = 300.0
    # Multiplier on our tokenizer's prompt estimate (other providers' tokenizers differ).
    spend_prompt_margin: float = 1.25

    # model="auto": classifier exported by eval/ (needs the "router" extra installed).
    router_model_path: str = "gateway/routing/router_model.json"
    router_threads: int = 2

    # Public playground: POST /demo/keys issues throwaway keys with tiny limits.
    demo_enabled: bool = False
    demo_spend_cap_usd: Decimal = Decimal("0.02")
    demo_rpm: int = 10
    demo_tpm: int = 20_000
    demo_keys_per_hour: int = 30
    demo_models: str = "auto,groq-gpt-oss-20b,groq-gpt-oss-120b,gemini-flash-lite,or-llama-8b"

    cache_ttl_s: float = 3600.0
    # Max time a request waits for an identical in-flight request before calling upstream itself.
    cache_wait_timeout_s: float = 30.0
    key_cache_ttl_s: float = 60.0
    key_cache_max_entries: int = 10_000
    log_queue_size: int = 10_000
    log_batch_size: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()


def secret_or_none(value: SecretStr | None) -> str | None:
    """Treat unset and empty-string secrets the same way."""
    if value is None:
        return None
    raw = value.get_secret_value().strip()
    return raw or None
