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

    MAX_RETRIES = 3

    def __init__(
        self,
        pool: UserbotPool,
        registry: SourceRegistry,
        cache: CacheManager,
    ) -> None:
        self._pool = pool
        self._registry = registry
        self._cache = cache

    # ── Поиск треков ──────────────────────────────────────────────────────────

    async def search(self, ctx: SearchContext) -> SearchResult:
        cached = await self._cache.get_search(ctx.query)
        if cached:
            logger.debug("Cache hit: %r", ctx.query)
            await self._cache.increment_popular(ctx.query)
            return cached

        sources = self._get_sources(ctx.preferred_source)
        if not sources:
            raise SourceUnavailableError("Нет доступных источников")

        last_error: Exception | None = None

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
        for attempt in range(self.MAX_RETRIES):
            userbot = await self._pool.acquire_userbot()
            if not userbot:
                wait = 2 ** attempt
                logger.warning(
                    "Нет свободных userbots, ждём %ds (попытка %d)",
                    wait, attempt + 1,
                )
                await asyncio.sleep(wait)
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

    # ── Получение / доставка аудио ────────────────────────────────────────────

    async def get_audio(
        self,
        track: Track,
        user_id: int,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        """
        Получает аудио для трека.

        Args:
            track: трек для получения аудио.
            user_id: ID пользователя (для кэша).
            target_chat_id: если передан, userbot доставляет аудио напрямую
                в этот чат (copy_message).  AudioFile.delivered будет True,
                и вызывающий код НЕ должен звать answer_audio.

        BUG FIX: file_id из Pyrogram-сессии не принимается главным ботом.
        Передавайте target_chat_id чтобы userbot доставил аудио напрямую.
        """
        # Проверяем кэш (только если не нужна прямая доставка)
        if track.source_track_id and target_chat_id is None:
            cached = await self._cache.get_audio(track.source_track_id)
            if cached:
                logger.debug("Audio cache hit: %r", track.title)
                return cached

        sources = self._get_sources()
        last_error: Exception | None = None

        for source in sources:
            userbot = await self._pool.acquire_userbot()
            if not userbot:
                raise SourceUnavailableError("Нет свободных userbots")

            try:
                if hasattr(source, "_client"):
                    source._client = userbot.client  # type: ignore[attr-defined]

                audio = await source.get_audio(track, target_chat_id=target_chat_id)
                await self._pool.release_userbot(userbot)

                # Кэшируем только если не было прямой доставки
                # (file_id userbot-сессии не валиден для главного бота)
                if not audio.delivered:
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

    # ── Вспомогательное ───────────────────────────────────────────────────────

    def _get_sources(self, preferred: str | None = None) -> list[MusicSource]:
        available = self._registry.get_available()
        if not preferred:
            return available
        preferred_src = [s for s in available if s.name == preferred]
        rest = [s for s in available if s.name != preferred]
        return preferred_src + rest
