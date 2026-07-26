import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import admin, search, start, subscription
from bot.handlers import relay
from bot.handlers import admin_sources
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.middlewares.subscription import SubscriptionMiddleware
from bot.middlewares.i18n import I18nMiddleware
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
from bot.handlers import settings as settings_handler, favorites, popular

logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    # ── Redis ────────────────────────────────────────────────────────────────────
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    storage = RedisStorage(redis=redis)
    cache = CacheManager(redis)

    # ── Репозиторий использует фабрику сессий (session-per-operation) ─────────
    repo = UserbotRepository(session_factory=async_session_factory)
    pool = UserbotPool(repo)

    # ── Bot (create early to get bot ID) ─────────────────────────────────────
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot_info = await bot.get_me()
    bot_id = bot_info.id
    logger.info("Бот @%s (id=%d)", bot_info.username, bot_id)

    # ── Источники ──────────────────────────────────────────────────────────────
    registry = SourceRegistry()
    registry.register(VKMusicBotSource(
        client=None,  # type: ignore[arg-type]
        priority=10,
        relay_bot_id=bot_id,
    ))

    search_manager = SearchManager(pool=pool, registry=registry, cache=cache)
    queue = QueueManager(search_manager=search_manager, cache=cache)

    await pool.start()
    await queue.start()

    # Регистрируем ID userbot-ов для relay-хендлера
    userbot_ids = set()
    for entry in pool.list_userbots():
        # Получаем Telegram ID аккаунта userbot
        try:
            me = await entry.client.get_me()
            userbot_ids.add(me.id)
        except Exception as e:
            logger.warning("Не удалось получить ID userbot #%d: %s", entry.id, e)
    relay.register_userbot_ids(userbot_ids)

    # ── Dispatcher ─────────────────────────────────────────────────────────────
    dp = Dispatcher(storage=storage)

    # Передаём зависимости через workflow_data
    dp["cache"] = cache
    dp["queue"] = queue
    dp["search_manager"] = search_manager
    dp["pool"] = pool

    # ── Middlewares ────────────────────────────────────────────────────────────
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.message.middleware(RateLimitMiddleware(cache))
    dp.message.middleware(I18nMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(I18nMiddleware())

    # ── Роутеры ───────────────────────────────────────────────────────────────
    # IMPORTANT: relay должен быть ПЕРВЫМ, чтобы перехватить аудио от userbot
    # до того, как AuthMiddleware попытается зарегистрировать userbot как пользователя.
    dp.include_router(relay.router)
    dp.include_router(start.router)
    dp.include_router(search.router)
    dp.include_router(subscription.router)
    # admin_sources MUST be before admin.router to intercept admin:sources callbacks
    dp.include_router(admin_sources.router)
    dp.include_router(admin.router)
    dp.include_router(inline.router)
    dp.include_router(inline_download.router)
    dp.include_router(inline_feedback.router)
    dp.include_router(settings_handler.router)
    dp.include_router(favorites.router)
    dp.include_router(popular.router)

    # ── Запуск ────────────────────────────────────────────────────────────────
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
