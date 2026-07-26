"""
Глобальные фикстуры pytest.
Фикс БАГ 10: убран устаревший event_loop fixture — asyncio_mode=auto достаточен.
"""
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from infrastructure.database.models import Base


@pytest_asyncio.fixture(scope="function")
async def db_engine():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(db_engine) -> AsyncGenerator[AsyncSession, None]:
    factory = async_sessionmaker(
        bind=db_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    async with factory() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def fake_redis() -> AsyncGenerator[FakeRedis, None]:
    redis = FakeRedis()
    yield redis
    await redis.flushall()
    await redis.aclose()


@pytest.fixture
def mock_pyrogram_client() -> AsyncMock:
    client = AsyncMock()
    client.start   = AsyncMock()
    client.stop    = AsyncMock()
    client.send_message     = AsyncMock()
    client.get_chat         = AsyncMock()
    client.get_chat_history = AsyncMock()
    return client


@pytest.fixture
def mock_bot() -> AsyncMock:
    bot = AsyncMock()
    bot.get_me       = AsyncMock(return_value=MagicMock(username="test_bot"))
    bot.send_message = AsyncMock()
    return bot
