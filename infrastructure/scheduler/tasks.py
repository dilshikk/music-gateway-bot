"""
Функции планировщика.
Каждая функция — независимая, идемпотентная задача.
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from infrastructure.database.models import Search, SearchStatus, Userbot, UserbotStatus
from infrastructure.database.session import async_session_factory

logger = logging.getLogger(__name__)


# ── 1. Сброс дневных счётчиков ────────────────────────────────────────────────

async def _reset_daily_counters(pool) -> None:
    """Сбрасывает requests_today для всех userbots в 00:00 UTC."""
    logger.info("[Scheduler] Сброс дневных счётчиков userbots")
    async with async_session_factory() as session:
        await session.execute(
            update(Userbot).values(requests_today=0)
        )
        await session.commit()

    # Обновляем in-memory модели в пуле
    for entry in pool.list_userbots():
        entry.model.requests_today = 0

    logger.info("[Scheduler] Дневные счётчики сброшены")


# ── 2. Очистка просроченного кэша ─────────────────────────────────────────────

async def _cleanup_expired_cache(cache) -> None:
    """
    Redis сам удаляет ключи по TTL.
    Здесь очищаем sorted set популярных запросов от мусора
    и пересчитываем топ.
    """
    logger.info("[Scheduler] Очистка кэша")
    try:
        # Удаляем записи с нулевым score из популярных (архивные)
        await cache._redis.zremrangebyscore("popular:queries", 0, 0)

        # Обновляем TTL на popular:queries если он ещё жив
        ttl = await cache._redis.ttl("popular:queries")
        if 0 < ttl < 600:  # меньше 10 минут — продлеваем
            from config.settings import settings
            await cache._redis.expire("popular:queries", settings.CACHE_POPULAR_TTL)

        logger.info("[Scheduler] Кэш очищен")
    except Exception as e:
        logger.error("[Scheduler] Ошибка очистки кэша: %s", e)


# ── 3. Авто-восстановление userbots ──────────────────────────────────────────

async def _recover_userbots(pool) -> None:
    """
    Проверяет userbots в статусе FLOOD_WAIT:
    если flood_wait_until уже прошёл — переводит в IDLE.
    """
    now = datetime.now(timezone.utc)
    recovered = 0

    async with async_session_factory() as session:
        result = await session.execute(
            select(Userbot).where(
                Userbot.status == UserbotStatus.FLOOD_WAIT,
                Userbot.flood_wait_until <= now,
            )
        )
        expired = list(result.scalars().all())

        for ub in expired:
            ub.status           = UserbotStatus.IDLE
            ub.flood_wait_until = None
            session.add(ub)
            recovered += 1

            # Синхронизируем in-memory объект в пуле
            entry = next(
                (e for e in pool.list_userbots() if e.id == ub.id), None
            )
            if entry:
                entry.model.status           = UserbotStatus.IDLE
                entry.model.flood_wait_until = None

        if recovered:
            await session.commit()
            logger.info("[Scheduler] Восстановлено userbots: %d", recovered)

    # Проверяем ERROR userbots — пытаемся перезапустить (не чаще раза в 5 мин)
    for entry in pool.list_userbots():
        if entry.model.status == UserbotStatus.ERROR:
            logger.info("[Scheduler] Перезапуск userbot #%d", entry.id)
            await pool.restart_userbot(entry.id)


# ── 4. Синхронизация статистики источников ────────────────────────────────────

async def _sync_source_stats(registry, engine: AsyncEngine) -> None:
    """Сохраняет in-memory статистику источников в БД."""
    from infrastructure.database.models import Source

    logger.info("[Scheduler] Синхронизация статистики источников")
    async with async_session_factory() as session:
        for source in registry.all():
            result = await session.execute(
                select(Source).where(Source.bot_username == source.bot_username)
            )
            db_source = result.scalar_one_or_none()
            if db_source:
                db_source.success_count   = source._success_count
                db_source.error_count     = source._error_count
                db_source.avg_response_ms = round(source.avg_response_ms, 2)
                session.add(db_source)

        await session.commit()
    logger.info("[Scheduler] Статистика источников синхронизирована")


# ── 5. Архивация старых поисков ───────────────────────────────────────────────

async def _archive_old_searches(engine: AsyncEngine) -> None:
    """
    Удаляет из таблицы searches записи старше 30 дней.
    Перед удалением проверяет что они завершены (не PENDING).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    logger.info("[Scheduler] Архивация поисков старше %s", cutoff.date())

    async with async_session_factory() as session:
        result = await session.execute(
            delete(Search)
            .where(
                Search.created_at < cutoff,
                Search.status.in_([
                    SearchStatus.DONE,
                    SearchStatus.FAILED,
                    SearchStatus.CACHED,
                ])
            )
            .returning(Search.id)
        )
        deleted = len(result.fetchall())
        await session.commit()

    logger.info("[Scheduler] Удалено старых поисков: %d", deleted)


# ── 6. Сброс счётчиков ошибок ─────────────────────────────────────────────────

async def _reset_error_counts(pool) -> None:
    """
    Сбрасывает error_count для userbots в IDLE статусе.
    Даём чистый старт каждое утро.
    """
    logger.info("[Scheduler] Сброс счётчиков ошибок userbots")
    async with async_session_factory() as session:
        await session.execute(
            update(Userbot)
            .where(Userbot.status == UserbotStatus.IDLE)
            .values(error_count=0)
        )
        await session.commit()

    for entry in pool.list_userbots():
        if entry.model.status == UserbotStatus.IDLE:
            entry.model.error_count = 0

    logger.info("[Scheduler] Счётчики ошибок сброшены")
