"""
Тесты для core/queue_manager.py — SearchTask и lazy Future initialisation.

BUG FIX: asyncio.get_event_loop().create_future() в default поле dataclass
вызывался вне async-контекста, что приводило к DeprecationWarning / RuntimeError
в Python 3.10+. Теперь Future создаётся лениво через __post_init__
с asyncio.get_running_loop().
"""
import asyncio

import pytest

from core.queue_manager import SearchTask, TaskPriority, TaskStatus
from core.search_manager import SearchContext


def make_ctx(user_id: int = 1, query: str = "test") -> SearchContext:
    return SearchContext(query=query, user_id=user_id)


class TestSearchTaskFuture:
    async def test_task_created_inside_event_loop(self) -> None:
        """Task must be creatable inside a running loop without errors."""
        task = SearchTask(
            task_id="abc",
            ctx=make_ctx(),
            priority=TaskPriority.NORMAL,
        )
        assert task._future is not None
        assert isinstance(task._future, asyncio.Future)

    async def test_future_not_done_initially(self) -> None:
        task = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        assert not task._future.done()

    async def test_resolve_sets_result(self) -> None:
        from sources.base import SearchResult

        task = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        result = SearchResult(tracks=[], total=0, query="test")
        task.resolve(result)

        assert task._future.done()
        assert await task._future is result
        assert task.status == TaskStatus.DONE

    async def test_reject_sets_exception(self) -> None:
        task = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        task.reject("Something went wrong")

        assert task._future.done()
        with pytest.raises(Exception, match="Something went wrong"):
            await task._future
        assert task.status == TaskStatus.FAILED

    async def test_expire_sets_timeout_error(self) -> None:
        task = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        task.expire()

        assert task._future.done()
        with pytest.raises(asyncio.TimeoutError):
            await task._future
        assert task.status == TaskStatus.TIMEOUT

    async def test_resolve_idempotent(self) -> None:
        """Повторный вызов resolve не должен выбрасывать исключение."""
        from sources.base import SearchResult

        task   = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        result = SearchResult(tracks=[], total=0, query="test")
        task.resolve(result)
        task.resolve(result)  # второй вызов должен быть безопасен

    async def test_reject_idempotent(self) -> None:
        task = SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
        task.reject("first error")
        task.reject("second error")  # должен быть безопасен

    def test_task_cannot_be_created_outside_loop(self) -> None:
        """
        Проверяем что вне event loop создание Task вызывает RuntimeError,
        что подтверждает правильность lazy init через __post_init__.
        """
        with pytest.raises(RuntimeError):
            SearchTask(task_id="abc", ctx=make_ctx(), priority=TaskPriority.NORMAL)
