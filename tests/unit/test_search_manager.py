"""
Тесты SearchManager — кэш, fallback, выбор источника.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from core.cache_manager import CacheManager
from core.search_manager import SearchContext, SearchManager
from core.userbot_pool import UserbotEntry, UserbotPool
from sources.base import (
    AudioFile,
    MusicSource,
    SearchResult,
    SourceFloodWaitError,
    SourceUnavailableError,
    Track,
)
from sources.registry import SourceRegistry
from tests.factories import UserbotFactory
from infrastructure.database.models import UserbotStatus


def make_track(title: str = "Test Track") -> Track:
    return Track(
        title=title,
        artist="Test Artist",
        duration=200,
        size=5_000_000,
        source_track_id="a:123:1",
    )


def make_search_result(query: str = "test", tracks: int = 3) -> SearchResult:
    return SearchResult(
        tracks=[make_track(f"Track {i}") for i in range(tracks)],
        total=tracks,
        query=query,
        source_name="Test Source",
    )


class _MockSource(MusicSource):
    name         = "Mock Source"
    bot_username = "mock_bot"
    source_type  = "telegram_bot"

    def __init__(self, result: SearchResult | None = None, **kwargs):
        super().__init__(**kwargs)
        self._result = result or make_search_result()
        self._client = AsyncMock()

    async def search(self, query: str, page: int = 1) -> SearchResult:
        return self._result

    async def get_audio(self, track: Track) -> AudioFile:
        return AudioFile(
            telegram_file_id="BQACAgI",
            telegram_unique_id="unique_test",
            title=track.title,
            artist=track.artist,
            duration=track.duration,
            size=track.size,
        )

    async def health_check(self) -> bool:
        return True


class _FailingSource(_MockSource):
    name = "Failing Source"

    async def search(self, query: str, page: int = 1) -> SearchResult:
        raise SourceUnavailableError("Source is down")


@pytest_asyncio.fixture
async def cache(fake_redis) -> CacheManager:
    return CacheManager(fake_redis)


@pytest.fixture
def mock_pool() -> AsyncMock:
    pool = AsyncMock(spec=UserbotPool)

    model  = UserbotFactory.build(id=1, status=UserbotStatus.IDLE)
    entry  = MagicMock(spec=UserbotEntry)
    entry.id     = 1
    entry.model  = model
    entry.client = AsyncMock()

    pool.acquire_userbot = AsyncMock(return_value=entry)
    pool.release_userbot = AsyncMock()
    pool.handle_flood_wait = AsyncMock()
    return pool


@pytest.fixture
def registry() -> SourceRegistry:
    r = SourceRegistry()
    r.register(_MockSource(priority=10))
    return r


@pytest.fixture
def search_manager(mock_pool, registry, cache) -> SearchManager:
    return SearchManager(pool=mock_pool, registry=registry, cache=cache)


class TestSearchCache:
    async def test_cache_miss_calls_source(
        self,
        search_manager: SearchManager,
        mock_pool: AsyncMock,
    ) -> None:
        ctx = SearchContext(query="eminem", user_id=1)
        result = await search_manager.search(ctx)

        assert result is not None
        mock_pool.acquire_userbot.assert_called_once()

    async def test_cache_hit_skips_source(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
        mock_pool: AsyncMock,
    ) -> None:
        # Прогреваем кэш
        cached_result = make_search_result("eminem")
        await cache.set_search("eminem", cached_result)

        ctx = SearchContext(query="eminem", user_id=1)
        result = await search_manager.search(ctx)

        # Userbot не должен был использоваться
        mock_pool.acquire_userbot.assert_not_called()
        assert result.total == cached_result.total

    async def test_result_saved_to_cache(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
    ) -> None:
        ctx = SearchContext(query="queen", user_id=1)
        await search_manager.search(ctx)

        cached = await cache.get_search("queen")
        assert cached is not None


class TestFallback:
    async def test_fallback_to_second_source(
        self,
        mock_pool: AsyncMock,
        cache: CacheManager,
    ) -> None:
        """Если первый источник падает — используется второй."""
        registry = SourceRegistry()
        failing  = _FailingSource(priority=10)
        working  = _MockSource(priority=5)
        failing.name = "Failing"
        working.name = "Working"

        registry.register(failing)
        registry.register(working)

        manager = SearchManager(pool=mock_pool, registry=registry, cache=cache)
        ctx     = SearchContext(query="test fallback", user_id=1)

        result = await manager.search(ctx)
        assert result is not None
        assert len(result.tracks) > 0

    async def test_all_sources_fail_raises(
        self,
        mock_pool: AsyncMock,
        cache: CacheManager,
    ) -> None:
        registry = SourceRegistry()
        failing1 = _FailingSource(priority=10)
        failing2 = _FailingSource(priority=5)
        failing1.name = "Failing1"
        failing2.name = "Failing2"

        registry.register(failing1)
        registry.register(failing2)

        manager = SearchManager(pool=mock_pool, registry=registry, cache=cache)
        ctx     = SearchContext(query="test all fail", user_id=1)

        with pytest.raises(SourceUnavailableError):
            await manager.search(ctx)

    async def test_no_sources_raises(
        self,
        mock_pool: AsyncMock,
        cache: CacheManager,
    ) -> None:
        registry = SourceRegistry()  # пустой
        manager  = SearchManager(pool=mock_pool, registry=registry, cache=cache)
        ctx      = SearchContext(query="no sources", user_id=1)

        with pytest.raises(SourceUnavailableError):
            await manager.search(ctx)


class TestGetAudio:
    async def test_returns_audio_file(
        self,
        search_manager: SearchManager,
    ) -> None:
        track = make_track()
        audio = await search_manager.get_audio(track, user_id=1)

        assert audio is not None
        assert audio.telegram_file_id == "BQACAgI"

    async def test_audio_cached_after_first_fetch(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
        mock_pool: AsyncMock,
    ) -> None:
        track = make_track()
        track.source_track_id = "unique_test"

        await search_manager.get_audio(track, user_id=1)
        call_count_after_first = mock_pool.acquire_userbot.call_count

        # Второй вызов — должен взять из кэша
        await search_manager.get_audio(track, user_id=1)
        assert mock_pool.acquire_userbot.call_count == call_count_after_first
