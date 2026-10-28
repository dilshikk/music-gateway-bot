"""
Тесты SearchManager — кэш, fallback, выбор источника, работа с аккаунтами.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

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


def make_entry(userbot_id: int = 1) -> MagicMock:
    model  = UserbotFactory.build(id=userbot_id, status=UserbotStatus.IDLE)
    entry  = MagicMock(spec=UserbotEntry)
    entry.id     = userbot_id
    entry.model  = model
    entry.client = AsyncMock()
    return entry


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

    async def get_audio(self, track: Track, target_chat_id: int | None = None) -> AudioFile:
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


class _FloodSource(_MockSource):
    name = "Flood Source"

    async def search(self, query: str, page: int = 1) -> SearchResult:
        raise SourceFloodWaitError(30)


@pytest_asyncio.fixture
async def cache(fake_redis) -> CacheManager:
    return CacheManager(fake_redis)


@pytest.fixture
def mock_pool() -> AsyncMock:
    pool = AsyncMock(spec=UserbotPool)
    pool.acquire_userbot = AsyncMock(return_value=make_entry(1))
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
        cached_result = make_search_result("eminem")
        await cache.set_search("eminem", cached_result)

        ctx = SearchContext(query="eminem", user_id=1)
        result = await search_manager.search(ctx)

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

    async def test_page_2_not_served_from_page_1_cache(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
        mock_pool: AsyncMock,
    ) -> None:
        """BUG: ключ кэша не учитывал страницу — листание возвращало первую страницу."""
        await cache.set_search("eminem", make_search_result("eminem"), page=1)

        await search_manager.search(SearchContext(query="eminem", user_id=1, page=2))

        mock_pool.acquire_userbot.assert_called_once()
        assert await cache.get_search("eminem", page=2) is not None

    async def test_tracks_stamped_with_userbot(
        self,
        search_manager: SearchManager,
    ) -> None:
        result = await search_manager.search(SearchContext(query="stamp", user_id=1))
        assert all(t.raw.get("userbot_id") == 1 for t in result.tracks)
        assert all(t.raw.get("source_name") == "Mock Source" for t in result.tracks)


class TestUserbotLifecycle:
    async def test_released_exactly_once_on_success(
        self,
        search_manager: SearchManager,
        mock_pool: AsyncMock,
    ) -> None:
        await search_manager.search(SearchContext(query="once", user_id=1))
        assert mock_pool.release_userbot.await_count == 1
        mock_pool.handle_flood_wait.assert_not_called()

    async def test_flood_wait_not_released_twice(
        self,
        mock_pool: AsyncMock,
        cache: CacheManager,
    ) -> None:
        """BUG: при FloodWait аккаунт освобождался дважды (flood + finally release)."""
        registry = SourceRegistry()
        registry.register(_FloodSource(priority=10))
        manager = SearchManager(pool=mock_pool, registry=registry, cache=cache)

        with pytest.raises(SourceUnavailableError):
            await manager.search(SearchContext(query="flood", user_id=1))

        assert mock_pool.handle_flood_wait.await_count == SearchManager.MAX_RETRIES
        mock_pool.release_userbot.assert_not_called()

    async def test_download_uses_same_userbot_as_search(
        self,
        search_manager: SearchManager,
        mock_pool: AsyncMock,
    ) -> None:
        result = await search_manager.search(SearchContext(query="same", user_id=1))
        mock_pool.acquire_userbot.reset_mock()

        track = result.tracks[0]
        track.source_track_id = "same:1"
        await search_manager.get_audio(track, user_id=1, target_chat_id=42)

        mock_pool.acquire_userbot.assert_awaited_with(userbot_id=1)

    async def test_no_free_userbot_raises(
        self,
        mock_pool: AsyncMock,
        registry: SourceRegistry,
        cache: CacheManager,
    ) -> None:
        mock_pool.acquire_userbot = AsyncMock(return_value=None)
        manager = SearchManager(pool=mock_pool, registry=registry, cache=cache)
        manager.ACQUIRE_TIMEOUT = 0

        with pytest.raises(SourceUnavailableError):
            await manager.search(SearchContext(query="busy", user_id=1))


class TestSourceBinding:
    def test_bind_returns_separate_copy_per_client(self) -> None:
        """BUG: общий source._client перезаписывался параллельными запросами."""
        source = _MockSource()
        c1, c2 = AsyncMock(), AsyncMock()

        b1 = source.bind(c1)
        b2 = source.bind(c2)

        assert b1 is not source and b2 is not source
        assert b1._client is c1
        assert b2._client is c2
        assert source._client is not c1 and source._client is not c2

    def test_bound_stats_go_to_parent(self) -> None:
        source = _MockSource()
        bound  = source.bind(AsyncMock())
        bound.record_success(100.0)
        bound.record_error()
        assert source._success_count == 1
        assert source._error_count == 1


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
        registry = SourceRegistry()
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
        mock_pool: AsyncMock,
    ) -> None:
        track = make_track()
        track.source_track_id = "unique_test"

        await search_manager.get_audio(track, user_id=1)
        call_count_after_first = mock_pool.acquire_userbot.call_count

        await search_manager.get_audio(track, user_id=1)
        assert mock_pool.acquire_userbot.call_count == call_count_after_first

    async def test_audio_cached_by_source_track_id(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
    ) -> None:
        """Кэш ищется по source_track_id — значит и сохраняться должен по нему."""
        track = make_track()
        track.source_track_id = "a:777:1"
        await search_manager.get_audio(track, user_id=1)
        assert await cache.get_audio("a:777:1") is not None
