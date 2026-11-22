"""Central configuration. All secrets come from environment variables / .env."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal, Optional

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Provider = Literal["openai", "anthropic", "gemini", "groq"]
Environment = Literal["development", "test", "production"]

DEFAULT_MODELS: dict[str, str] = {
    "openai": "gpt-6-luna",
    "anthropic": "claude-haiku-5-5",
    "gemini": "gemini-3.5-flash-lite",
    "groq": "openai/gpt-oss-120b",
}

# USD per 1M tokens: (input, output, cached_input). List prices checked 2026-11-21
# (sources + caveats in README section "LLM research"). Update this table when prices change.
MODEL_PRICING: dict[tuple[str, str], tuple[float, float, float]] = {
    ("openai", "gpt-6-luna"): (0.10, 0.50, 0.01),
    ("openai", "gpt-5.6-luna"): (0.20, 1.20, 0.02),
    ("openai", "gpt-5.6-terra"): (2.00, 12.00, 0.20),
    # Haiku 5.5 is priced 5x higher for prompts > 100K tokens; our prompts are ~7K.
    ("anthropic", "claude-haiku-5-5"): (0.10, 0.50, 0.01),
    ("anthropic", "claude-sonnet-5-5"): (2.00, 10.00, 0.20),
    ("gemini", "gemini-3.5-flash-lite"): (0.30, 2.50, 0.03),
    ("gemini", "gemini-3.1-flash-lite"): (0.25, 1.50, 0.025),
    # Introductory price, doubles on 2027-01-01.
    ("gemini", "gemini-3.8-flash"): (0.75, 3.75, 0.075),
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.60, 0.075),
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- LLM --------------------------------------------------------------
    llm_provider: Provider = "openai"
    llm_model: Optional[str] = None
    llm_fallback_provider: Optional[Provider] = None
    llm_fallback_model: Optional[str] = None
    openai_api_key: Optional[SecretStr] = None
    anthropic_api_key: Optional[SecretStr] = None
    gemini_api_key: Optional[SecretStr] = None
    groq_api_key: Optional[SecretStr] = None
    openai_reasoning_effort: Optional[str] = "low"
    gemini_thinking_level: Optional[str] = None
    llm_max_output_tokens: int = 8000
    llm_timeout_seconds: float = 90.0

    # --- Website fetching ---------------------------------------------------
    fetch_timeout_seconds: float = 12.0
    dns_timeout_seconds: float = 5.0
    fetch_max_bytes: int = 2_000_000
    max_redirects: int = 5
    max_extra_pages: int = 3
    max_content_chars: int = 20_000
    user_agent: str = "Mozilla/5.0 (compatible; LinkAnalysisBot/1.0)"

    # --- Cache / protection ---------------------------------------------------
    environment: Environment = "production"
    cache_ttl_seconds: int = 86_400
    cache_max_items: int = 256
    rate_limit_per_minute: int = 20
    trust_proxy_headers: bool = False
    verify_competitors: bool = True
    cors_origins: str = "*"
    allow_private_urls: bool = False  # DEV/TEST ONLY

    @model_validator(mode="after")
    def _validate_environment_and_security(self) -> Settings:
        if self.environment == "production" and self.allow_private_urls:
            raise ValueError(
                "Unsafe configuration: ALLOW_PRIVATE_URLS cannot be enabled in production environment. "
                "The application has failed closed to prevent SSRF vulnerabilities."
            )
        return self

    @field_validator("*", mode="before")
    @classmethod
    def _empty_string_is_none(cls, v):
        # `.env` lines like `LLM_MODEL=` should behave as "unset", not as "".
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    # ------------------------------------------------------------------ helpers
    def model_for(self, provider: str, explicit: Optional[str] = None) -> str:
        return explicit or DEFAULT_MODELS[provider]

    def api_key_for(self, provider: str) -> Optional[str]:
        secret = {
            "openai": self.openai_api_key,
            "anthropic": self.anthropic_api_key,
            "gemini": self.gemini_api_key,
            "groq": self.groq_api_key,
        }[provider]
        return secret.get_secret_value() if secret else None

    @property
    def primary_model(self) -> str:
        return self.model_for(self.llm_provider, self.llm_model)


@lru_cache
def get_settings() -> Settings:
    return Settings()
