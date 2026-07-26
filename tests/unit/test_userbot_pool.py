"""
Тесты UserbotPool.
Pyrogram Client полностью замокан.
"""
import pytest
import pytest_asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from infrastructure.database.models import Userbot, UserbotStatus
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from core.userbot_pool import UserbotEntry, UserbotPool
from tests.factories import UserbotFactory


def make_userbot_model(**kwargs) -> Userbot:
    ub = UserbotFactory.build(**kwargs)
    ub.id = kwargs.get("id", 1)
    return ub


@pytest.fixture
def mock_repo() -> AsyncMock:
    repo = AsyncMock(spec=UserbotRepository)
    repo.save = AsyncMock(side_effect=lambda obj: obj)
    return repo


@pytest.fixture
def mock_client() -> AsyncMock:
    client = AsyncMock()
    client.start  = AsyncMock()
    client.stop   = AsyncMock()
    return client


@pytest.fixture
def pool(mock_repo) -> UserbotPool:
    return UserbotPool(repo=mock_repo)


@pytest.fixture
def idle_entry(mock_client) -> UserbotEntry:
    model  = make_userbot_model(id=1, status=UserbotStatus.IDLE, weight=1)
    return UserbotEntry(model=model, client=mock_client)


class TestUserbotEntry:
    def test_is_available_when_idle(self, idle_entry: UserbotEntry) -> None:
        assert idle_entry.is_available is True

    def test_not_available_when_busy(self, idle_entry: UserbotEntry) -> None:
        idle_entry.model.status = UserbotStatus.BUSY
        assert idle_entry.is_available is False

    def test_not_available_when_over_limit(self, idle_entry: UserbotEntry) -> None:
        idle_entry.model.requests_today = idle_entry.model.daily_limit
        assert idle_entry.is_available is False

    async def test_acquire_success(self, idle_entry: UserbotEntry) -> None:
        acquired = await idle_entry.acquire()
        assert acquired is True

    async def test_acquire_twice_fails(self, idle_entry: UserbotEntry) -> None:
        await idle_entry.acquire()
        acquired_again = await idle_entry.acquire()
        assert acquired_again is False

    async def test_release_after_acquire(self, idle_entry: UserbotEntry) -> None:
        await idle_entry.acquire()
        idle_entry.release()
        # После release можно снова занять
        acquired = await idle_entry.acquire()
        assert acquired is True


class TestUserbotPool:
    async def test_acquire_returns_idle_userbot(
        self,
        pool: UserbotPool,
        idle_entry: UserbotEntry,
        mock_repo: AsyncMock,
    ) -> None:
        pool._pool[1] = idle_entry
        mock_repo.save = AsyncMock(return_value=idle_entry.model)

        userbot = await pool.acquire_userbot()
        assert userbot is not None
        assert userbot.id == 1

    async def test_acquire_returns_none_when_all_busy(
        self,
        pool: UserbotPool,
        mock_client: AsyncMock,
    ) -> None:
        busy_model  = make_userbot_model(id=2, status=UserbotStatus.BUSY)
        busy_entry  = UserbotEntry(model=busy_model, client=mock_client)
        pool._pool[2] = busy_entry

        userbot = await pool.acquire_userbot()
        assert userbot is None

    async def test_release_increments_counters(
        self,
        pool: UserbotPool,
        idle_entry: UserbotEntry,
        mock_repo: AsyncMock,
    ) -> None:
        pool._pool[1] = idle_entry
        await idle_entry.acquire()

        initial_today = idle_entry.model.requests_today
        initial_total = idle_entry.model.requests_total

        await pool.release_userbot(idle_entry)

        assert idle_entry.model.requests_today == initial_today + 1
        assert idle_entry.model.requests_total == initial_total + 1
        assert idle_entry.model.status         == UserbotStatus.IDLE

    async def test_handle_flood_wait_sets_status(
        self,
        pool: UserbotPool,
        idle_entry: UserbotEntry,
        mock_repo: AsyncMock,
    ) -> None:
        pool._pool[1] = idle_entry
        await idle_entry.acquire()

        await pool.handle_flood_wait(idle_entry, seconds=30)

        assert idle_entry.model.status == UserbotStatus.FLOOD_WAIT

    async def test_get_stats(
        self,
        pool: UserbotPool,
        mock_client: AsyncMock,
    ) -> None:
        pool._pool[1] = UserbotEntry(
            model=make_userbot_model(id=1, status=UserbotStatus.IDLE),
            client=mock_client,
        )
        pool._pool[2] = UserbotEntry(
            model=make_userbot_model(id=2, status=UserbotStatus.BUSY),
            client=mock_client,
        )
        pool._pool[3] = UserbotEntry(
            model=make_userbot_model(id=3, status=UserbotStatus.FLOOD_WAIT),
            client=mock_client,
        )

        stats = pool.get_stats()
        assert stats["total"] == 3
        assert stats["idle"]  == 1
        assert stats["busy"]  == 1
        assert stats["flood"] == 1

    def test_list_userbots(
        self,
        pool: UserbotPool,
        idle_entry: UserbotEntry,
    ) -> None:
        pool._pool[1] = idle_entry
        assert len(pool.list_userbots()) == 1
