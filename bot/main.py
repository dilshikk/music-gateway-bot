import asyncio
import logging
import logging.handlers
import os

asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import admin, search, start, subscription
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.middlewares.subscription import SubscriptionMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from config.settings import settings
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchManager
from core.userbot_pool import UserbotPool
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from infrastructure.database.session import async_session_factory
from sources.registry import SourceRegistry
from sources.vk_music_bot import VKMusicBotSource
from bot.handlers import inline, inline_download, inline_feedback
from bot.middlewares.i18n import I18nMiddleware
from bot.handlers import settings as settings_handler, favorites, popular


def _setup_logging() -> None:
    """Configure logging to both stdout and a rotating file at project root."""
    log_level = getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)

    # Rotating file handler — max 5 MB × 3 backup files → bot.log at project root
    log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.log")
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)

    root = logging.getLogger()
    root.setLevel(log_level)
    root.addHandler(console)
    root.addHandler(file_handler)


_setup_logging()
logger = logging.getLogger(__name__)


async def main() -> None:
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=False)
    storage = RedisStorage(redis=redis)
    cache = CacheManager(redis)

    db_session = async_session_factory()
    await db_session.__aenter__()

    try:
        repo = UserbotRepository(db_session)
        pool = UserbotPool(repo)
        registry = SourceRegistry()
        registry.register(VKMusicBotSource(client=None, priority=10))  # type: ignore[arg-type]

        search_manager = SearchManager(pool=pool, registry=registry, cache=cache)
        queue = QueueManager(search_manager=search_manager, cache=cache)

        await pool.start()
        await queue.start()

        bot = Bot(
            token=settings.BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        dp = Dispatcher(storage=storage)

        dp["cache"] = cache
        dp["queue"] = queue
        dp["search_manager"] = search_manager
        dp["pool"] = pool

        dp.message.middleware(ThrottleMiddleware())
        dp.message.middleware(AuthMiddleware())
        dp.message.middleware(RateLimitMiddleware(cache))
        dp.message.middleware(I18nMiddleware())
        dp.callback_query.middleware(AuthMiddleware())
        dp.callback_query.middleware(I18nMiddleware())

        dp.include_router(admin.router)
        dp.include_router(start.router)
        dp.include_router(subscription.router)
        dp.include_router(settings_handler.router)
        dp.include_router(favorites.router)
        dp.include_router(popular.router)
        dp.include_router(inline.router)
        dp.include_router(inline_download.router)
        dp.include_router(inline_feedback.router)
        dp.include_router(search.router)

        logger.info("Бот запущен")
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            await queue.stop()
            await pool.stop()
            await bot.session.close()
            logger.info("Бот остановлен")

    finally:
        await db_session.__aexit__(None, None, None)


if __name__ == "__main__":
    asyncio.run(main())
