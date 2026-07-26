"""
Конфигурация приложения через pydantic-settings.
Читает переменные из .env файла.
"""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Telegram Bot
    BOT_TOKEN:  str
    ADMIN_IDS:  list[int] = Field(default_factory=list)

    # Database
    DATABASE_URL: str
    DB_PASSWORD:  str = ""

    # Redis
    REDIS_URL: str = "redis://localhost:6379/0"

    # Pyrogram
    PYROGRAM_API_ID:   int = 0
    PYROGRAM_API_HASH: str = ""

    # FastAPI
    INTERNAL_API_KEY: str = "change_me_in_production"
    API_SECRET_KEY: str = "change-me-in-production"
    API_HOST:       str = "0.0.0.0"
    API_PORT:       int = 8000

    # Cache TTL
    CACHE_SEARCH_TTL:  int = 3600
    CACHE_AUDIO_TTL:   int = 86400
    CACHE_POPULAR_TTL: int = 86400

    # Rate limiting
    RATE_LIMIT_MINUTE: int = 5
    RATE_LIMIT_DAY:    int = 100

    # Scheduler
    SCHEDULER_TIMEZONE: str = "UTC"

    # Monitoring
    MONITOR_INTERVAL:  int = 30
    WATCHDOG_CHAT_ID:  int = 0

    # Inline mode
    INLINE_CACHE_TIME:  int   = 60
    INLINE_MAX_RESULTS: int   = 20
    INLINE_TIMEOUT:     float = 8.0
    INLINE_RATE_LIMIT:  int   = 30

    # Environment
    ENVIRONMENT: str  = "production"
    DEBUG:       bool = False


settings = Settings()