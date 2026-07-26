import asyncio
import logging

# BUG FIX: pyrogram 2.0.106 calls asyncio.get_event_loop() at import time.
# uvloop replaces the default event loop policy and raises RuntimeError when
# get_event_loop() is called before any loop has been created.
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

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # ── Redis ──────────────────────────────────────────────────────────────────
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=False)
    storage = RedisStorage(redis=redis)
    cache = CacheManager(redis)

    # ── БД + компоненты ───────────────────────────────────────────────────────
    # BUG FIX: раньше здесь открывалась одна db_session на весь lifecycle бота.
    # Это работало, но после перехода UserbotRepository на session-per-operation
    # нужно просто передавать фабрику сессий — не открывать сессию вручную.
    repo = UserbotRepository(session_factory=async_session_factory)
    pool = UserbotPool(repo)
    registry = SourceRegistry()
    registry.register(VKMusicBotSource(client=None, priority=10))  # type: ignore[arg-type]

    search_manager = SearchManager(pool=pool, registry=registry, cache=cache)
    queue = QueueManager(search_manager=search_manager, cache=cache)

    await pool.start()
    await queue.start()

    # ── Bot + Dispatcher ──────────────────────────────────────────────────
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=storage)

    dp["cache"] = cache
    dp["queue"] = queue
    dp["search_manager"] = search_manager
    dp["pool"] = pool

    # ── Middlewares ───────────────────────────────────────────────────────
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.message.middleware(RateLimitMiddleware(cache))
    dp.message.middleware(I18nMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(I18nMiddleware())

    # ── Роутеры ───────────────────────────────────────────────────────────
    # BUG FIX: admin router MUST be first so FSM states (AddUserbotStates,
    # BroadcastStates) have priority over the catch-all search handler
    # (F.text & ~F.text.startswith("/")) which would otherwise swallow
    # every plain-text message including phone numbers and api credentials.
    dp.include_router(admin.router)
    dp.include_router(start.router)
    dp.include_router(subscription.router)
    dp.include_router(settings_handler.router)
    dp.include_router(favorites.router)
    dp.include_router(popular.router)
    dp.include_router(inline.router)
    dp.include_router(inline_download.router)
    dp.include_router(inline_feedback.router)
    # search router last — its catch-all F.text handler must not intercept
    # FSM inputs or keyboard button texts handled by routers above
    dp.include_router(search.router)

    # ── Запуск ────────────────────────────────────────────────────────────
    logger.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await queue.stop()
        await pool.stop()
        await bot.session.close()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
