"""
Тесты CacheManager.
Полная изоляция — только fakeredis, без реального Redis.
"""
import pytest
import pytest_asyncio

from core.cache_manager import CacheManager
from sources.base import AudioFile, SearchResult, Track


@pytest_asyncio.fixture
async def cache(fake_redis) -> CacheManager:
    return CacheManager(fake_redis)


@pytest.fixture
def sample_track() -> Track:
    return Track(
        title="Bohemian Rhapsody",
        artist="Queen",
        duration=354,
        size=8_500_000,
        source_track_id="a:123:1",
        bitrate=320,
    )


@pytest.fixture
def sample_result(sample_track) -> SearchResult:
    return SearchResult(
        tracks=[sample_track],
        total=100,
        page=1,
        has_next=True,
        source_name="VK Music Bot",
        query="queen bohemian",
    )


@pytest.fixture
def sample_audio() -> AudioFile:
    return AudioFile(
        telegram_file_id="BQACAgIAAxkBAAIB",
        telegram_unique_id="unique_abc123",
        title="Bohemian Rhapsody",
        artist="Queen",
        duration=354,
        size=8_500_000,
    )


class TestSearchCache:
    async def test_miss_returns_none(self, cache: CacheManager) -> None:
        result = await cache.get_search("nonexistent query")
        assert result is None

    async def test_set_and_get(
        self,
        cache: CacheManager,
        sample_result: SearchResult,
    ) -> None:
        await cache.set_search("queen bohemian", sample_result)
        fetched = await cache.get_search("queen bohemian")

        assert fetched is not None
        assert fetched.total == 100
        assert fetched.page  == 1
        assert fetched.has_next is True
        assert len(fetched.tracks) == 1
        assert fetched.tracks[0].title == "Bohemian Rhapsody"

    async def test_case_insensitive_key(
        self,
        cache: CacheManager,
        sample_result: SearchResult,
    ) -> None:
        """Одинаковый кэш для 'Queen' и 'queen'."""
        await cache.set_search("Queen", sample_result)

        fetched_lower = await cache.get_search("queen")
        assert fetched_lower is not None

        fetched_upper = await cache.get_search("QUEEN")
        assert fetched_upper is not None

    async def test_whitespace_normalized(
        self,
        cache: CacheManager,
        sample_result: SearchResult,
    ) -> None:
        await cache.set_search("  eminem  ", sample_result)
        fetched = await cache.get_search("eminem")
        assert fetched is not None


class TestAudioCache:
    async def test_miss_returns_none(self, cache: CacheManager) -> None:
        result = await cache.get_audio("nonexistent_unique_id")
        assert result is None

    async def test_set_and_get(
        self,
        cache: CacheManager,
        sample_audio: AudioFile,
    ) -> None:
        await cache.set_audio(sample_audio)
        fetched = await cache.get_audio(sample_audio.telegram_unique_id)

        assert fetched is not None
        assert fetched.telegram_file_id    == sample_audio.telegram_file_id
        assert fetched.telegram_unique_id  == sample_audio.telegram_unique_id
        assert fetched.title               == sample_audio.title


class TestRateLimit:
    async def test_allows_within_limit(self, cache: CacheManager) -> None:
        for _ in range(5):
            allowed, retry_after = await cache.check_rate_limit(user_id=1)
            assert allowed is True
            assert retry_after == 0

    async def test_blocks_over_limit(self, cache: CacheManager) -> None:
        # Исчерпываем лимит
        for _ in range(5):
            await cache.check_rate_limit(user_id=2)

        # Следующий должен быть заблокирован
        allowed, retry_after = await cache.check_rate_limit(user_id=2)
        assert allowed is False
        assert retry_after > 0

    async def test_different_users_independent(self, cache: CacheManager) -> None:
        """Лимит одного пользователя не влияет на другого."""
        for _ in range(6):
            await cache.check_rate_limit(user_id=10)

        allowed, _ = await cache.check_rate_limit(user_id=11)
        assert allowed is True


class TestHistory:
    async def test_empty_history(self, cache: CacheManager) -> None:
        history = await cache.get_history(user_id=99)
        assert history == []

    async def test_add_and_get(self, cache: CacheManager) -> None:
        await cache.add_to_history(1, "eminem")
        await cache.add_to_history(1, "queen")
        await cache.add_to_history(1, "nirvana")

        history = await cache.get_history(1)
        assert len(history) == 3
        assert "nirvana" in history  # последний добавленный первый

    async def test_limit_50_entries(self, cache: CacheManager) -> None:
        for i in range(60):
            await cache.add_to_history(2, f"query_{i}")

        history = await cache.get_history(2, limit=100)
        assert len(history) <= 50

    async def test_clear_history(self, cache: CacheManager) -> None:
        await cache.add_to_history(3, "test")
        await cache.clear_history(3)
        history = await cache.get_history(3)
        assert history == []


class TestPopular:
    async def test_increment_and_get(self, cache: CacheManager) -> None:
        await cache.increment_popular("eminem")
        await cache.increment_popular("eminem")
        await cache.increment_popular("queen")

        popular = await cache.get_popular(limit=10)
        queries = [q for q, _ in popular]

        assert "eminem" in queries
        assert "queen"  in queries

    async def test_sorted_by_count(self, cache: CacheManager) -> None:
        for _ in range(5):
            await cache.increment_popular("eminem")
        for _ in range(2):
            await cache.increment_popular("queen")

        popular = await cache.get_popular(limit=2)
        assert popular[0][0] == "eminem"
        assert popular[1][0] == "queen"
