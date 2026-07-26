import asyncio
import logging
import time
from dataclasses import dataclass

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
    1. Проверить кэш → вернуть если есть
    2. Выбрать источник по приоритету
    3. Выбрать свободный userbot из пула
    4. Выполнить поиск через источник
    5. При ошибке — fallback на следующий источник
    6. Сохранить в кэш
    7. Вернуть результат
    """

    # Поиск через @vkmusic_bot занимает ~12-15s.
    # При одном userbot пагинация/скачивание должны дождаться его освобождения.
    # 10 попыток × 3s = 30s максимального ожидания — достаточно.
    MAX_RETRIES = 10
    RETRY_WAIT  = 3.0  # секунд между попытками взять userbot

    def __init__(
        self,
        pool: UserbotPool,
        registry: SourceRegistry,
        cache: CacheManager,
    ) -> None:
        self._pool     = pool
        self._registry = registry
        self._cache    = cache

    # ── Поиск треков ──────────────────────────────────────────────────────

    async def search(self, ctx: SearchContext) -> SearchResult:
        # 1. Кэш
        cached = await self._cache.get_search(ctx.query)
        if cached:
            logger.debug("Cache hit: %r", ctx.query)
            await self._cache.increment_popular(ctx.query)
            return cached

        # 2. Получить источники
        sources = self._get_sources(ctx.preferred_source)
        if not sources:
            raise SourceUnavailableError("Нет доступных источников")

        last_error: Exception | None = None

        # 3. Fallback-цепочка по источникам
        for source in sources:
            try:
                result = await self._search_with_retry(source, ctx)
                await self._cache.set_search(ctx.query, result)
                await self._cache.increment_popular(ctx.query)
                await self._cache.add_to_history(ctx.user_id, ctx.query)
                return result

            except (SourceUnavailableError, SourceTimeoutError) as e:
                logger.warning(
                    "Источник %r недоступен, пробуем следующий: %s",
                    source.name, e,
                )
                last_error = e
                continue

        raise SourceUnavailableError(
            f"Все источники недоступны. Последняя ошибка: {last_error}"
        )

    async def _search_with_retry(
        self,
        source: MusicSource,
        ctx: SearchContext,
    ) -> SearchResult:
        """Выполняет поиск с retry через разные userbots."""
        for attempt in range(self.MAX_RETRIES):
            userbot = await self._pool.acquire_userbot()
            if not userbot:
                logger.warning(
                    "Нет свободных userbots, ждём %.1fs (попытка %d/%d)",
                    self.RETRY_WAIT, attempt + 1, self.MAX_RETRIES,
                )
                await asyncio.sleep(self.RETRY_WAIT)
                continue

            try:
                return await self._execute_search(source, userbot, ctx)

            except SourceFloodWaitError as e:
                await self._pool.handle_flood_wait(userbot, e.seconds)
                continue

            except Exception:
                await self._pool.release_userbot(userbot)
                raise

        raise SourceUnavailableError("Превышено количество попыток")

    async def _execute_search(
        self,
        source: MusicSource,
        userbot: UserbotEntry,
        ctx: SearchContext,
    ) -> SearchResult:
        start = time.monotonic()
        try:
            if hasattr(source, "_client"):
                source._client = userbot.client  # type: ignore[attr-defined]

            result = await source.search(ctx.query, page=ctx.page)
            source.record_success((time.monotonic() - start) * 1000)
            return result

        except SourceFloodWaitError:
            source.record_error()
            raise

        except Exception as e:
            source.record_error()
            raise SourceUnavailableError(str(e)) from e

        finally:
            await self._pool.release_userbot(userbot)

    # ── Получение аудио ──────────────────────────────────────────────────────

    async def get_audio(
        self,
        track: Track,
        user_id: int,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        """
        Получить аудиофайл.

        target_chat_id — Telegram chat_id пользователя. Если указан,
        userbot перешлёт аудио напрямую в чат пользователя
        и возвращает AudioFile с already_sent=True.
        """
        # Проверяем кэш file_id
        if track.source_track_id:
            cached = await self._cache.get_audio(track.source_track_id)
            if cached:
                logger.debug("Audio cache hit: %r", track.title)
                return cached

        sources = self._get_sources()
        last_error: Exception | None = None

        for source in sources:
            # Ждём свободный userbot с теми же retry-параметрами
            userbot = None
            for attempt in range(self.MAX_RETRIES):
                userbot = await self._pool.acquire_userbot()
                if userbot:
                    break
                logger.warning(
                    "[get_audio] Нет свободных userbots, ждём %.1fs (попытка %d/%d)",
                    self.RETRY_WAIT, attempt + 1, self.MAX_RETRIES,
                )
                await asyncio.sleep(self.RETRY_WAIT)

            if not userbot:
                raise SourceUnavailableError("Нет свободных userbots")

            try:
                if hasattr(source, "_client"):
                    source._client = userbot.client  # type: ignore[attr-defined]

                audio = await source.get_audio(
                    track,
                    target_chat_id=target_chat_id,
                )
                await self._pool.release_userbot(userbot)

                # Кэшируем file_id
                await self._cache.set_audio(audio)
                return audio

            except SourceFloodWaitError as e:
                await self._pool.handle_flood_wait(userbot, e.seconds)
                last_error = e
                continue

            except Exception as e:
                await self._pool.release_userbot(userbot)
                last_error = e
                continue

        raise SourceUnavailableError(
            f"Не удалось получить аудио: {last_error}"
        )

    # ── Вспомогательное ───────────────────────────────────────────────

    def _get_sources(self, preferred: str | None = None) -> list[MusicSource]:
        available = self._registry.get_available()
        if not preferred:
            return available
        preferred_src = [s for s in available if s.name == preferred]
        rest          = [s for s in available if s.name != preferred]
        return preferred_src + rest
