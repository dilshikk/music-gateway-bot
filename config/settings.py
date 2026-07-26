"""
Конфигурация приложения через pydantic-settings.
Читает переменные из .env файла.
"""
import logging
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram Bot
    BOT_TOKEN: str
    # Поддерживает оба формата в .env:
    #   ADMIN_IDS=123456789
    #   ADMIN_IDS=[123456789,987654321]
    #   ADMIN_IDS=123456789,987654321
    ADMIN_IDS: list[int] = Field(default_factory=list)

    @field_validator("ADMIN_IDS", mode="before")
    @classmethod
    def parse_admin_ids(cls, v: object) -> list[int]:
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            v = v.strip().strip("[]")
            if not v:
                return []
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return []

    # Database
    DATABASE_URL: str
    DB_PASSWORD: str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Pyrogram
    PYROGRAM_API_ID: int = 0
    PYROGRAM_API_HASH: str = ""

    # FastAPI
    INTERNAL_API_KEY: str = "change_me_in_production"
    API_SECRET_KEY: str = "change-me-in-production"
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000

    # Cache TTL (seconds)
    CACHE_SEARCH_TTL: int = 3600
    CACHE_AUDIO_TTL: int = 86400   # also used as CACHE_FILE_TTL
    CACHE_POPULAR_TTL: int = 86400

    # Rate limiting
    RATE_LIMIT_MINUTE: int = 5
    RATE_LIMIT_DAY: int = 100

    # Scheduler
    SCHEDULER_TIMEZONE: str = "UTC"

    # Monitoring
    MONITOR_INTERVAL: int = 30
    WATCHDOG_CHAT_ID: int = 0

    # Inline mode
    INLINE_CACHE_TIME: int = 60
    INLINE_MAX_RESULTS: int = 20
    INLINE_TIMEOUT: float = 8.0
    INLINE_RATE_LIMIT: int = 30

    # Environment
    ENVIRONMENT: str = "production"
    DEBUG: bool = False

    # Logging
    LOG_LEVEL: str = "INFO"

    @property
    def log_level_int(self) -> int:
        return getattr(logging, self.LOG_LEVEL.upper(), logging.INFO)

    @property
    def redis_url(self) -> str:
        return self.REDIS_URL


settings = Settings()
