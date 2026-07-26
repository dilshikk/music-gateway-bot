import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from redis.asyncio import Redis

from bot.handlers import admin, search, start, subscription
from bot.handlers import admin_sources
from bot.middlewares.auth import AuthMiddleware
from bot.middlewares.rate_limit import RateLimitMiddleware
from bot.middlewares.i18n import I18nMiddleware
from bot.middlewares.throttle import ThrottleMiddleware
from config.settings import settings
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchManager
from core.userbot_pool import UserbotPool
from infrastructure.database.repositories.source_repo import SourceRepository
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


async def _build_registry() -> SourceRegistry:
    """
    Загружает все источники из БД и регистрирует их в памяти.

    Это единственный правильный способ инициализации реестра:
    имена/username берутся из БД — именно по ним работают
    sync_enabled() и unregister() в admin_sources.py.
    """
    registry = SourceRegistry()

    async with async_session_factory() as session:
        repo = SourceRepository(session)
        # Создаём дефолтный VK-источник если его ещё нет
        await repo.get_or_create_vk()
        sources = await repo.get_all()

    for src in sources:
        mem_source = VKMusicBotSource(
            client=None,   # type: ignore[arg-type]
            priority=src.priority,
            timeout=src.timeout,
            enabled=src.enabled,
        )
        # ВАЖНО: имя должно точно совпадать с src.name из БД,
        # иначе sync_enabled() не найдёт источник в реестре
        mem_source.name         = src.name
        mem_source.bot_username = src.bot_username
        registry.register(mem_source)
        logger.info(
            "Registered source from DB: id=%d name='%s' @%s enabled=%s priority=%d",
            src.id, src.name, src.bot_username, src.enabled, src.priority,
        )
        print(
            f"[REGISTRY] id={src.id} name='{src.name}' "
            f"@{src.bot_username} enabled={src.enabled} priority={src.priority}"
        )

    logger.info("SourceRegistry loaded %d source(s) from DB", len(registry))
    print(f"[REGISTRY] Total: {len(registry)} source(s)")
    return registry


async def main() -> None:
    # ── Redis ────────────────────────────────────────────────────────────────────
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    storage = RedisStorage(redis=redis)
    cache = CacheManager(redis)

    # ── Компоненты ────────────────────────────────────────────────────────────
    db_session = async_session_factory()
    await db_session.__aenter__()

    try:
        repo = UserbotRepository(db_session)
        pool = UserbotPool(repo)

        # Загружаем реестр источников из БД (все записи, с правильными именами)
        registry = await _build_registry()

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

        # Передаём зависимости через workflow_data
        dp["cache"]          = cache
        dp["queue"]          = queue
        dp["search_manager"] = search_manager
        dp["pool"]           = pool
        # registry нужен admin_sources для sync_enabled() при тогле ON/OFF
        dp["registry"]       = registry

        # ── Middlewares ───────────────────────────────────────────────────────
        dp.message.middleware(ThrottleMiddleware())
        dp.message.middleware(AuthMiddleware())
        dp.message.middleware(RateLimitMiddleware(cache))
        dp.message.middleware(I18nMiddleware())
        dp.callback_query.middleware(AuthMiddleware())
        dp.callback_query.middleware(I18nMiddleware())

        # ── Роутеры ───────────────────────────────────────────────────────────
        # IMPORTANT: favorites / popular / settings должны быть ДО search.router,
        # иначе широкий фильтр F.text в search перехватит reply-кнопки
        # (⭐ Избранное, 🔥 Popular, ⚙️ Settings) как поисковые запросы.
        dp.include_router(start.router)
        dp.include_router(favorites.router)
        dp.include_router(popular.router)
        dp.include_router(settings_handler.router)
        dp.include_router(search.router)
        dp.include_router(subscription.router)

        # admin_sources MUST be before admin.router:
        # lambda-фильтры на "admin:src:*" должны перехватываться первыми
        dp.include_router(admin_sources.router)
        dp.include_router(admin.router)

        dp.include_router(inline.router)
        dp.include_router(inline_download.router)
        dp.include_router(inline_feedback.router)

        # ── Запуск ────────────────────────────────────────────────────────────
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
