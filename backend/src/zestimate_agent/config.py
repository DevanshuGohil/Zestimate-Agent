"""Settings loaded from environment / .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from platformdirs import user_data_dir
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parents[2] / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM (Mistral) ---
    mistral_api_key: SecretStr = Field(..., description="Mistral API key (free tier OK)")
    mistral_model: str = Field(default="mistral-small-latest")
    mistral_fallback_model: str = Field(default="open-mistral-7b")

    # --- Primary provider (RapidAPI Zillow wrapper) ---
    rapidapi_key: SecretStr = Field(..., description="RapidAPI key for Zillow wrapper")
    rapidapi_host: str = Field(default="zillow56.p.rapidapi.com")

    # --- Normalization (optional) ---
    google_maps_api_key: SecretStr | None = Field(default=None)

    # --- DirectProvider (optional proxy) ---
    proxy_url: SecretStr | None = Field(default=None)

    # --- Cache ---
    cache_ttl_hours: int = Field(default=1, ge=1)
    cache_failure_ttl_hours: int = Field(default=6, ge=1)
    cache_dir: Path = Field(
        default_factory=lambda: Path(user_data_dir("zestimate_agent", appauthor=False))
    )

    # --- Observability ---
    log_level: LogLevel = Field(default="INFO")
    langsmith_api_key: SecretStr | None = Field(default=None)
    langsmith_tracing: bool = Field(default=False)

    # --- Rate limiting ---
    rate_limit_lookup: str = Field(default="10/minute")
    rate_limit_cache: str = Field(default="5/minute")

    # --- Request timeout ---
    request_timeout_seconds: int = Field(default=30, ge=5, le=300)

    # --- Agent behavior ---
    max_retry_attempts: int = Field(default=2, ge=1, le=5)

    @property
    def cache_db_path(self) -> Path:
        return self.cache_dir / "cache.db"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached settings accessor. Call `get_settings.cache_clear()` in tests."""
    return Settings()  # type: ignore[call-arg]
