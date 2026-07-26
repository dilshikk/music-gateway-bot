"""
Alembic env.py для music-gateway-bot.
Поддерживает asyncpg (async) и синхронный режим для autogenerate.
DATABASE_URL читается из .env файла или переменной окружения.
"""
import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── Загрузка .env ─────────────────────────────────────────────────────────────
# Ищем .env начиная от папки migrations вверх до корня проекта
def _load_dotenv() -> None:
    """Загружает .env файл без внешних зависимостей."""
    root = Path(__file__).resolve().parent
    for _ in range(5):  # идём вверх максимум 5 уровней
        env_file = root / ".env"
        if env_file.exists():
            with open(env_file, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    # Не перезаписываем уже установленные переменные
                    if key and key not in os.environ:
                        os.environ[key] = value
            break
        root = root.parent

_load_dotenv()

# ── Импорт моделей для autogenerate ──────────────────────────────────────────
# ВАЖНО: все модели должны быть импортированы до вызова Base.metadata
from infrastructure.database.models import Base  # noqa: F401

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# ── URL базы данных ───────────────────────────────────────────────────────────
def get_sync_url() -> str:
    """
    Возвращает синхронный URL (psycopg2) для offline/autogenerate режима.
    asyncpg не поддерживается в синхронном Alembic.
    """
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL не задан. Создайте файл .env в корне проекта:\n"
            "DATABASE_URL=postgresql+asyncpg://postgres:пароль@localhost:5432/music_gateway"
        )
    return (
        url
        .replace("postgresql+asyncpg://", "postgresql+psycopg2://")
        .replace("postgresql://", "postgresql+psycopg2://")
    )

def get_async_url() -> str:
    """Возвращает asyncpg URL для online-миграций."""
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL не задан в .env")
    if not url.startswith("postgresql+asyncpg://"):
        url = (
            url
            .replace("postgresql+psycopg2://", "postgresql+asyncpg://")
            .replace("postgresql://", "postgresql+asyncpg://")
        )
    return url


# ── Offline миграции ──────────────────────────────────────────────────────────
def run_migrations_offline() -> None:
    context.configure(
        url=get_sync_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online миграции ───────────────────────────────────────────────────────────
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_async_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


# ── Точка входа ───────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
