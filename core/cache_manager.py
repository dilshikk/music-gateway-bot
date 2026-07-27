import hashlib
import json
import time
from dataclasses import asdict

from redis.asyncio import Redis

from config.settings import settings
from sources.base import AudioFile, SearchResult, Track

# Версия схемы кэша поиска.
# Увеличить при любом изменении формата Track.raw, чтобы автоматически
# инвалидировать все старые записи в Redis без ручного FLUSHDB.
# Текущая версия: 2 — добавлены search_chat_id, search_msg_id, parsed_at.
_SEARCH_CACHE_VERSION = 2


class CacheManager:
    """
    Все операции с Redis-кэшем в одном месте.

    Ключи:
      search:v{N}:{hash}    — SearchResult (список треков), N = _SEARCH_CACHE_VERSION
      file:{unique_id}      — AudioFile (file_id для пересылки)
      user:{id}:rate:{win}  — sliding window rate limit
      user:{id}:history     — история поиска (sorted set)
      popular:queries       — топ запросов (sorted set)
      userbot:{id}:status   — статус userbot в реальном времени
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    # ── Поиск ─────────────────────────────────────────────────────────────────

    async def get_search(self, query: str) -> SearchResult | None:
        key = self._search_key(query)
        raw = await self._redis.get(key)
        if not raw:
            return None
        data = json.loads(raw)
        tracks = [Track(**t) for t in data["tracks"]]
        return SearchResult(
            tracks=tracks,
            total=data["total"],
            page=data["page"],
            has_next=data["has_next"],
            source_name=data["source_name"],
            query=data["query"],
        )

    async def set_search(self, query: str, result: SearchResult) -> None:
        key = self._search_key(query)
        data = {
            "tracks": [asdict(t) for t in result.tracks],
            "total": result.total,
            "page": result.page,
            "has_next": result.has_next,
            "source_name": result.source_name,
            "query": result.query,
        }
        await self._redis.set(key, json.dumps(data), ex=settings.CACHE_SEARCH_TTL)

    # ── Аудиофайлы ────────────────────────────────────────────────────────────

    async def get_audio(self, unique_id: str) -> AudioFile | None:
        key = f"file:{unique_id}"
        raw = await self._redis.get(key)
        if not raw:
            return None
        return AudioFile(**json.loads(raw))

    async def set_audio(self, audio: AudioFile) -> None:
        key = f"file:{audio.telegram_unique_id}"
        await self._redis.set(
            key,
            json.dumps(asdict(audio)),
            ex=settings.CACHE_FILE_TTL,
        )

    # ── Rate Limit (универсальный sliding window) ─────────────────────────────

    async def check_rate_limit(
        self,
        user_id: int,
        max_requests: int = 5,
        window: int = 60,
        key_suffix: str = "",
    ) -> tuple[bool, int]:
        """
        Sliding window rate limit.

        Args:
            user_id: Telegram user id
            max_requests: максимум запросов за window секунд
            window: размер окна в секундах
            key_suffix: суффикс ключа, например ":inline"

        Returns:
            (allowed, retry_after_seconds)
        """
        bucket = int(time.time()) // window
        key = f"user:{user_id}:rate{key_suffix}:{bucket}"

        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, window)

        if count > max_requests:
            ttl = await self._redis.ttl(key)
            return False, max(ttl, 1)

        return True, 0

    # ── История пользователя ──────────────────────────────────────────────────

    async def add_to_history(self, user_id: int, query: str) -> None:
        key = f"user:{user_id}:history"
        score = time.time()
        await self._redis.zadd(key, {query: score})
        # Храним последние 50 запросов
        await self._redis.zremrangebyrank(key, 0, -51)
        await self._redis.expire(key, 60 * 60 * 24 * 30)  # 30 дней

    async def get_history(self, user_id: int, limit: int = 10) -> list[str]:
        key = f"user:{user_id}:history"
        items = await self._redis.zrevrange(key, 0, limit - 1)
        return [i.decode() if isinstance(i, bytes) else i for i in items]

    async def clear_history(self, user_id: int) -> None:
        await self._redis.delete(f"user:{user_id}:history")

    # ── Популярные запросы ────────────────────────────────────────────────────

    async def increment_popular(self, query: str) -> None:
        key = "popular:queries"
        normalized = query.strip().lower()
        await self._redis.zincrby(key, 1, normalized)
        await self._redis.expire(key, settings.CACHE_POPULAR_TTL)

    async def get_popular(self, limit: int = 10) -> list[tuple[str, float]]:
        key = "popular:queries"
        items = await self._redis.zrevrange(key, 0, limit - 1, withscores=True)
        return [
            (i[0].decode() if isinstance(i[0], bytes) else i[0], i[1])
            for i in items
        ]

    # ── Статус userbot ────────────────────────────────────────────────────────

    async def set_userbot_status(self, userbot_id: int, status: str) -> None:
        await self._redis.set(f"userbot:{userbot_id}:status", status, ex=120)

    async def get_userbot_status(self, userbot_id: int) -> str | None:
        val = await self._redis.get(f"userbot:{userbot_id}:status")
        return val.decode() if val else None

    # ── Вспомогательное ───────────────────────────────────────────────────────

    @staticmethod
    def _search_key(query: str) -> str:
        """
        Ключ кэша поиска включает версию схемы (_SEARCH_CACHE_VERSION).
        При изменении формата Track.raw достаточно увеличить константу —
        все старые ключи (без версии или с меньшей версией) перестают находиться,
        и кэш автоматически заполняется свежими данными.
        """
        normalized = query.strip().lower()
        h = hashlib.md5(normalized.encode()).hexdigest()
        return f"search:v{_SEARCH_CACHE_VERSION}:{h}"

    async def ping(self) -> bool:
        try:
            return await self._redis.ping()
        except Exception:
            return False
