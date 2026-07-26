"""
Интеграционный тест полного поиска:
  fakeredis + SQLite + mock Pyrogram
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchContext, SearchManager
from core.userbot_pool import UserbotEntry, UserbotPool
from infrastructure.database.models import UserbotStatus
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from sources.base import AudioFile, MusicSource, SearchResult, Track
from sources.registry import SourceRegistry
from tests.factories import UserbotFactory


class _IntegrationSource(MusicSource):
    name         = "Integration Source"
    bot_username = "int_bot"
    source_type  = "telegram_bot"

    async def search(self, query: str, page: int = 1) -> SearchResult:
        return SearchResult(
            tracks=[
                Track(
                    title=f"{query} — result",
                    artist="Test",
                    duration=180,
                    size=4_000_000,
                    source_track_id="int:001:1",
                )
            ],
            total=1,
            query=query,
            source_name=self.name,
        )

    async def get_audio(self, track: Track) -> AudioFile:
        return AudioFile(
            telegram_file_id="INT_FILE_ID",
            telegram_unique_id="int_unique",
            title=track.title,
            artist=track.artist,
            duration=track.duration,
            size=track.size,
        )

    async def health_check(self) -> bool:
        return True


@pytest_asyncio.fixture
async def full_stack(fake_redis, db_session):
    """Собирает полный стек компонентов для интеграционного теста."""
    cache    = CacheManager(fake_redis)
    repo     = UserbotRepository(db_session)
    pool     = AsyncMock(spec=UserbotPool)

    model  = UserbotFactory.build(id=1, status=UserbotStatus.IDLE)
    entry  = MagicMock(spec=UserbotEntry)
    entry.id     = 1
    entry.model  = model
    entry.client = AsyncMock()

    pool.acquire_userbot = AsyncMock(return_value=entry)
    pool.release_userbot = AsyncMock()
    pool.handle_flood_wait = AsyncMock()

    registry = SourceRegistry()
    registry.register(_IntegrationSource(priority=10))

    search = SearchManager(pool=pool, registry=registry, cache=cache)
    queue  = QueueManager(search_manager=search, cache=cache)

    await queue.start()
    yield {"search": search, "queue": queue, "cache": cache, "pool": pool}
    await queue.stop()


class TestFullSearchFlow:
    async def test_search_returns_result(self, full_stack: dict) -> None:
        search = full_stack["search"]
        ctx    = SearchContext(query="integration test", user_id=1)

        result = await search.search(ctx)

        assert result is not None
        assert len(result.tracks) == 1
        assert "integration test" in result.tracks[0].title

    async def test_second_search_from_cache(self, full_stack: dict) -> None:
        search = full_stack["search"]
        pool   = full_stack["pool"]
        ctx    = SearchContext(query="cache test", user_id=1)

        await search.search(ctx)
        pool.acquire_userbot.reset_mock()

        await search.search(ctx)
        pool.acquire_userbot.assert_not_called()

    async def test_queue_delivers_result(self, full_stack: dict) -> None:
        queue = full_stack["queue"]
        ctx   = SearchContext(query="queue test", user_id=2)

        task   = await queue.enqueue(ctx)
        result = await queue.wait_for_result(task, timeout=15)

        assert result is not None
        assert len(result.tracks) == 1

    async def test_get_audio_after_search(self, full_stack: dict) -> None:
        search = full_stack["search"]
        ctx    = SearchContext(query="audio test", user_id=3)

        result = await search.search(ctx)
        track  = result.tracks[0]
        audio  = await search.get_audio(track, user_id=3)

        assert audio.telegram_file_id == "INT_FILE_ID"
