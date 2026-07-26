"""
Alembic env.py для music-gateway-bot.
Поддерживает asyncpg (async) и синхронный режим для autogenerate.
DATABASE_URL читается из переменной окружения или .env файла.
"""
import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── Импорт моделей для autogenerate ──────────────────────────────────────────
# ВАЖНО: все модели должны быть импортированы до вызова Base.metadata
from infrastructure.database.models import Base  # noqa: F401 — регистрирует все таблицы

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

# Настройка логирования из alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Метаданные для autogenerate
target_metadata = Base.metadata

# ── URL базы данных ───────────────────────────────────────────────────────────
def get_url() -> str:
    """
    Приоритет:
    1. Переменная окружения DATABASE_URL
    2. Значение из alembic.ini (sqlalchemy.url)
    """
    url = os.getenv("DATABASE_URL")
    if url:
        # asyncpg не поддерживается в синхронном режиме Alembic —
        # для offline/autogenerate заменяем на psycopg2
        return url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    return config.get_main_option("sqlalchemy.url", "")


# ── Offline миграции (без подключения к БД) ───────────────────────────────────
def run_migrations_offline() -> None:
    """Генерирует SQL-скрипт без подключения к базе данных."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online миграции (с подключением к БД) ────────────────────────────────────
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
    """Запускает миграции через asyncpg."""
    # Используем asyncpg URL как есть
    url = os.getenv("DATABASE_URL", config.get_main_option("sqlalchemy.url", ""))
    if not url.startswith("postgresql+asyncpg://"):
        url = url.replace("postgresql+psycopg2://", "postgresql+asyncpg://")
        url = url.replace("postgresql://", "postgresql+asyncpg://")

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = url

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Точка входа для online-режима."""
    asyncio.run(run_async_migrations())


# ── Точка входа ───────────────────────────────────────────────────────────────
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
