"""
Точка входа для отдельного воркер-процесса.
Запускается как: python -m core.worker
"""
import asyncio
import logging

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from config.settings import settings
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchManager
from core.userbot_pool import UserbotPool
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from infrastructure.database.session import async_session_factory
from sources.registry import SourceRegistry
from sources.vk_music_bot import VKMusicBotSource

logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger(__name__)


async def create_components():
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    cache = CacheManager(redis)

    async with async_session_factory() as session:
        repo     = UserbotRepository(session)
        pool     = UserbotPool(repo)
        registry = SourceRegistry()

        # Регистрируем источники
        # Клиент передаётся позже из пула при каждом запросе
        registry.register(
            VKMusicBotSource(
                client=None,   # type: ignore[arg-type]
                priority=10,
            )
        )

        search = SearchManager(pool=pool, registry=registry, cache=cache)
        queue  = QueueManager(search_manager=search, cache=cache)

        return pool, queue, cache


async def main() -> None:
    logger.info("Запуск воркера...")
    pool, queue, cache = await create_components()

    await pool.start()
    await queue.start()

    logger.info("Воркер готов")

    try:
        await asyncio.Event().wait()  # Держим процесс живым
    except asyncio.CancelledError:
        pass
    finally:
        await queue.stop()
        await pool.stop()
        logger.info("Воркер остановлен")


if __name__ == "__main__":
    asyncio.run(main())
