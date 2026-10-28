import asyncio
import logging
import time
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import TypeVar

from core.cache_manager import CacheManager
from core.userbot_pool import UserbotEntry, UserbotPool
from sources.base import (
    AudioFile,
    MusicSource,
    SearchResult,
    SourceFloodWaitError,
    SourceTimeoutError,
    SourceUnavailableError,
    Track,
)
from sources.registry import SourceRegistry

logger = logging.getLogger(__name__)

T = TypeVar("T")


@dataclass
class SearchContext:
    query: str
    user_id: int
    page: int = 1
    preferred_source: str | None = None


class SearchManager:
    """
    Центральный оркестратор поиска.

    Алгоритм:
    1. Проверить кэш (с учётом страницы) → вернуть если есть
    2. Выбрать источник по приоритету
    3. Дождаться свободного аккаунта (один аккаунт = один запрос за раз)
    4. Выполнить поиск через копию источника, привязанную к этому аккаунту
    5. При ошибке — fallback на следующий источник
    6. Сохранить в кэш
    """

    MAX_RETRIES = 3
    # Сколько ждать свободный аккаунт, прежде чем сдаться
    ACQUIRE_TIMEOUT = 25.0
    ACQUIRE_POLL = 0.5

    def __init__(
        self,
        pool: UserbotPool,
        registry: SourceRegistry,
        cache: CacheManager,
    ) -> None:
        self._pool     = pool
        self._registry = registry
        self._cache    = cache

    # ── Поиск треков ──────────────────────────────────────────────────────────

    async def search(self, ctx: SearchContext) -> SearchResult:
        cached = await self._cache.get_search(ctx.query, ctx.page)
        if cached:
            logger.debug("Cache hit: %r page=%d", ctx.query, ctx.page)
            if ctx.page == 1:
                await self._cache.increment_popular(ctx.query)
            return cached

        sources = self._get_sources(ctx.preferred_source)
        if not sources:
            raise SourceUnavailableError("Нет доступных источников")

        last_error: Exception | None = None

        for source in sources:
            try:
                result = await self._search_with_retry(source, ctx)
            except (SourceUnavailableError, SourceTimeoutError) as e:
                logger.warning(
                    "Источник %r недоступен, пробуем следующий: %s",
                    source.name, e,
                )
                last_error = e
                continue

            await self._cache.set_search(ctx.query, result, ctx.page)
            # Листание страниц не должно накручивать популярность и историю
            if ctx.page == 1:
                await self._cache.increment_popular(ctx.query)
                await self._cache.add_to_history(ctx.user_id, ctx.query)
            return result

        raise SourceUnavailableError(
            f"Все источники недоступны. Последняя ошибка: {last_error}"
        )

    async def _search_with_retry(
        self,
        source: MusicSource,
        ctx: SearchContext,
    ) -> SearchResult:
        """Выполняет поиск; при FloodWait пробует другой аккаунт."""
        if not self._needs_userbot(source):
            return await self._run_search(source, ctx, None)

        for attempt in range(self.MAX_RETRIES):
            userbot = await self._acquire()
            if userbot is None:
                raise SourceUnavailableError("Нет свободных аккаунтов")
            try:
                return await self._with_userbot(
                    userbot, self._run_search(source, ctx, userbot)
                )
            except SourceFloodWaitError as e:
                logger.warning(
                    "Userbot #%d FloodWait %ds, пробуем другой (попытка %d)",
                    userbot.id, e.seconds, attempt + 1,
                )
                continue

        raise SourceUnavailableError("Превышено количество попыток")

    async def _run_search(
        self,
        source: MusicSource,
        ctx: SearchContext,
        userbot: UserbotEntry | None,
    ) -> SearchResult:
        bound = source.bind(userbot.client) if userbot else source
        try:
            result = await bound.search(ctx.query, page=ctx.page)
        except (SourceFloodWaitError, SourceUnavailableError, SourceTimeoutError):
            raise
        except Exception as e:
            raise SourceUnavailableError(str(e)) from e

        # Запоминаем, какой аккаунт и источник нашли трек: кнопки результатов
        # существуют только в чате этого аккаунта, скачивать надо через него же.
        for track in result.tracks:
            track.raw["source_name"] = source.name
            if userbot is not None:
                track.raw["userbot_id"] = userbot.id
        return result

    # ── Получение аудио ───────────────────────────────────────────────────────

    async def get_audio(
        self,
        track: Track,
        user_id: int,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        if track.source_track_id:
            cached = await self._cache.get_audio(track.source_track_id)
            if cached:
                logger.debug("Audio cache hit: %r", track.title)
                return cached

        preferred = track.raw.get("source_name")
        sources = self._get_sources(preferred if isinstance(preferred, str) else None)
        last_error: Exception | None = None

        for source in sources:
            try:
                audio = await self._fetch_audio(source, track, target_chat_id)
            except Exception as e:
                logger.warning("get_audio через %r не удался: %s", source.name, e)
                last_error = e
                continue

            # file_id, полученный аккаунтом, не подходит для Bot API —
            # кэшируем только то, что бот сможет отправить сам.
            if not audio.already_sent:
                await self._cache.set_audio(audio, alias=track.source_track_id or None)
            return audio

        raise SourceUnavailableError(
            f"Не удалось получить аудио: {last_error}"
        )

    async def _fetch_audio(
        self,
        source: MusicSource,
        track: Track,
        target_chat_id: int | None,
    ) -> AudioFile:
        if not self._needs_userbot(source):
            return await source.get_audio(track, target_chat_id=target_chat_id)

        owner_id = track.raw.get("userbot_id")
        userbot = await self._acquire(owner_id if isinstance(owner_id, int) else None)
        if userbot is None:
            raise SourceUnavailableError("Аккаунт, выполнивший поиск, недоступен")

        bound = source.bind(userbot.client)
        return await self._with_userbot(
            userbot, bound.get_audio(track, target_chat_id=target_chat_id)
        )

    # ── Работа с аккаунтами ───────────────────────────────────────────────────

    async def _acquire(self, userbot_id: int | None = None) -> UserbotEntry | None:
        """Ждёт свободный аккаунт (или конкретный) до ACQUIRE_TIMEOUT секунд."""
        deadline = time.monotonic() + self.ACQUIRE_TIMEOUT
        while True:
            userbot = await self._pool.acquire_userbot(userbot_id=userbot_id)
            if userbot is not None or time.monotonic() >= deadline:
                return userbot
            await asyncio.sleep(self.ACQUIRE_POLL)

    async def _with_userbot(self, userbot: UserbotEntry, op: Awaitable[T]) -> T:
        """
        Выполняет операцию и освобождает аккаунт РОВНО один раз.
        При FloodWait аккаунт уходит в паузу (handle_flood_wait сам снимает lock).
        """
        try:
            result = await op
        except SourceFloodWaitError as e:
            await self._pool.handle_flood_wait(userbot, e.seconds)
            raise
        except BaseException:
            await self._pool.release_userbot(userbot)
            raise
        await self._pool.release_userbot(userbot)
        return result

    # ── Вспомогательное ───────────────────────────────────────────────────────

    @staticmethod
    def _needs_userbot(source: MusicSource) -> bool:
        return source.source_type == "telegram_bot"

    def _get_sources(self, preferred: str | None = None) -> list[MusicSource]:
        available = self._registry.get_available()
        if not preferred:
            return available
        preferred_src = [s for s in available if s.name == preferred]
        rest          = [s for s in available if s.name != preferred]
        return preferred_src + rest
