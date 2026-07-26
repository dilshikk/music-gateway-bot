import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import admin, search, start, subscription
from bot.handlers import relay
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.rate_limit import RateLimitMiddleware
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
    # ── Redis ─────────────────────────────────────────────────────────────────
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    storage = RedisStorage(redis=redis)
    cache = CacheManager(redis)

    # ── Bot (создаём рано — нужен bot_id для relay) ───────────────────────────
    bot = Bot(
        token=settings.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot_info = await bot.get_me()
    bot_id = bot_info.id
    logger.info("Бот @%s (id=%d)", bot_info.username, bot_id)
    print(f"[main] бот @{bot_info.username} id={bot_id}")

    # ── Userbot pool ──────────────────────────────────────────────────────────
    repo = UserbotRepository(session_factory=async_session_factory)
    pool = UserbotPool(repo)
    await pool.start()
    print(f"[main] userbot pool запущен")

    # ── Источник музыки с очередью ────────────────────────────────────────────
    # Один userbot + asyncio.Queue — задачи обрабатываются строго по одной,
    # никакой гонки за ответами @vkmusic_bot между пользователями.
    source = VKMusicBotSource(
        client=None,  # type: ignore[arg-type]  — клиент выдаётся из pool
        priority=10,
        relay_bot_id=bot_id,
    )
    await source.start()
    print(f"[main] source worker запущен")

    registry = SourceRegistry()
    registry.register(source)

    search_manager = SearchManager(pool=pool, registry=registry, cache=cache)
    queue = QueueManager(search_manager=search_manager, cache=cache)
    await queue.start()
    print(f"[main] queue manager запущен")

    # ── Регистрируем ID userbot-ов для relay-хендлера ─────────────────────────
    userbot_ids: set[int] = set()
    for entry in pool.list_userbots():
        try:
            me = await entry.client.get_me()
            userbot_ids.add(me.id)
            print(f"[main] userbot id={me.id} @{me.username}")
        except Exception as e:
            logger.warning("Не удалось получить ID userbot #%d: %s", entry.id, e)
    relay.register_userbot_ids(userbot_ids)
    print(f"[main] зарегистрировано userbot ids: {userbot_ids}")

    # ── Dispatcher ────────────────────────────────────────────────────────────
    dp = Dispatcher(storage=storage)

    dp["cache"]          = cache
    dp["queue"]          = queue
    dp["search_manager"] = search_manager
    dp["pool"]           = pool

    # ── Middlewares ───────────────────────────────────────────────────────────
    dp.message.middleware(ThrottleMiddleware())
    dp.message.middleware(AuthMiddleware())
    dp.message.middleware(RateLimitMiddleware(cache))
    dp.message.middleware(I18nMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.callback_query.middleware(I18nMiddleware())

    # ── Роутеры ───────────────────────────────────────────────────────────────
    # IMPORTANT: relay ПЕРВЫМ — перехватывает аудио от userbot до AuthMiddleware
    dp.include_router(relay.router)
    dp.include_router(start.router)
    dp.include_router(search.router)
    dp.include_router(subscription.router)
    dp.include_router(admin.router)
    dp.include_router(inline.router)
    dp.include_router(inline_download.router)
    dp.include_router(inline_feedback.router)
    dp.include_router(settings_handler.router)
    dp.include_router(favorites.router)
    dp.include_router(popular.router)

    # ── Запуск ────────────────────────────────────────────────────────────────
    logger.info("Бот запущен")
    print("[main] бот запущен, начинаем polling")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await source.stop()
        await queue.stop()
        await pool.stop()
        await bot.session.close()
        logger.info("Бот остановлен")
        print("[main] бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
