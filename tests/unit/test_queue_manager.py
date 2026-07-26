"""
Тесты QueueManager — приоритеты, таймауты, rate limit.
"""
import asyncio
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch

from core.cache_manager import CacheManager
from core.queue_manager import QueueManager, TaskPriority, TaskStatus
from core.search_manager import SearchContext, SearchManager
from sources.base import SearchResult, Track


def make_search_ctx(user_id: int = 1, query: str = "test") -> SearchContext:
    return SearchContext(query=query, user_id=user_id)


@pytest_asyncio.fixture
async def cache(fake_redis) -> CacheManager:
    return CacheManager(fake_redis)


@pytest_asyncio.fixture
async def mock_search() -> AsyncMock:
    search = AsyncMock(spec=SearchManager)
    search.search = AsyncMock(return_value=SearchResult(
        tracks=[],
        total=0,
        query="test",
    ))
    return search


@pytest_asyncio.fixture
async def queue(mock_search, cache) -> QueueManager:
    q = QueueManager(search_manager=mock_search, cache=cache)
    await q.start()
    yield q
    await q.stop()


class TestEnqueue:
    async def test_enqueue_returns_task(
        self,
        queue: QueueManager,
        cache: CacheManager,
        fake_redis,
    ) -> None:
        # Убеждаемся что rate limit не блокирует
        task = await queue.enqueue(make_search_ctx(user_id=100))
        assert task is not None
        assert task.status in (TaskStatus.PENDING, TaskStatus.PROCESSING, TaskStatus.DONE)

    async def test_premium_gets_high_priority(
        self,
        queue: QueueManager,
    ) -> None:
        task_normal  = await queue.enqueue(make_search_ctx(user_id=200), is_premium=False)
        task_premium = await queue.enqueue(make_search_ctx(user_id=201), is_premium=True)

        assert task_normal.priority  == TaskPriority.NORMAL
        assert task_premium.priority == TaskPriority.HIGH

    async def test_queue_overflow_raises(
        self,
        mock_search: AsyncMock,
        cache: CacheManager,
    ) -> None:
        q = QueueManager(search_manager=mock_search, cache=cache)
        q.MAX_QUEUE_SIZE = 2
        await q.start()

        try:
            # Останавливаем воркеры чтобы очередь не опустошалась
            for w in q._workers:
                w.cancel()

            await q.enqueue(make_search_ctx(user_id=300))
            await q.enqueue(make_search_ctx(user_id=301))

            with pytest.raises(OverflowError):
                await q.enqueue(make_search_ctx(user_id=302))
        finally:
            await q.stop()

    async def test_rate_limit_raises(
        self,
        mock_search: AsyncMock,
        fake_redis,
    ) -> None:
        """Проверяем что rate limit вызывает PermissionError."""
        cache = CacheManager(fake_redis)
        q = QueueManager(search_manager=mock_search, cache=cache)
        await q.start()

        try:
            # Исчерпываем лимит вручную
            with patch.object(
                cache,
                "check_rate_limit",
                AsyncMock(return_value=(False, 60)),
            ):
                with pytest.raises(PermissionError):
                    await q.enqueue(make_search_ctx(user_id=400))
        finally:
            await q.stop()


class TestWaitForResult:
    async def test_resolves_with_result(
        self,
        queue: QueueManager,
    ) -> None:
        task   = await queue.enqueue(make_search_ctx(user_id=500))
        result = await queue.wait_for_result(task, timeout=10)

        assert isinstance(result, SearchResult)

    async def test_timeout_raises(
        self,
        mock_search: AsyncMock,
        cache: CacheManager,
    ) -> None:
        # Замедляем search чтобы получить таймаут
        async def slow_search(*args, **kwargs):
            await asyncio.sleep(10)
            return SearchResult(tracks=[], total=0, query="")

        mock_search.search = slow_search

        q = QueueManager(search_manager=mock_search, cache=cache)
        q.WORKERS_COUNT = 1
        await q.start()

        try:
            task = await q.enqueue(make_search_ctx(user_id=600))
            with pytest.raises(asyncio.TimeoutError):
                await q.wait_for_result(task, timeout=0.1)
        finally:
            await q.stop()


class TestGetPosition:
    async def test_position_of_first_task(
        self,
        mock_search: AsyncMock,
        cache: CacheManager,
    ) -> None:
        q = QueueManager(search_manager=mock_search, cache=cache)
        # Не запускаем воркеров — задачи остаются в очереди

        try:
            task = await q.enqueue(make_search_ctx(user_id=700))
            pos  = q.get_position(task.task_id)
            assert pos is not None
            assert pos >= 1
        finally:
            pass  # не вызываем stop, воркеры не запущены

    async def test_position_none_for_unknown(
        self,
        queue: QueueManager,
    ) -> None:
        pos = queue.get_position("nonexistent-task-id")
        assert pos is None
