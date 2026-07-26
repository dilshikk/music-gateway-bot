"""
Регрессионные тесты для core/userbot_pool.py.

Покрывают два исправленных бага:
1. handle_flood_wait сохранял некорректное время (second=0, microsecond=0)
2. acquire() имел TOCTOU race condition — два корутина могли оба пройти
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.userbot_pool import UserbotEntry, UserbotPool
from infrastructure.database.models import Userbot, UserbotStatus
from tests.factories import UserbotFactory


def make_model(**kwargs) -> Userbot:
    m    = UserbotFactory.build(**kwargs)
    m.id = kwargs.get("id", 1)
    return m


def make_entry(**kwargs) -> UserbotEntry:
    model  = make_model(**kwargs)
    client = AsyncMock()
    return UserbotEntry(model=model, client=client)


# ─── Тест: flood_wait_until содержит точное время ───────────────────────────

class TestHandleFloodWaitTimestamp:
    """BUG: handle_flood_wait устанавливал .second=0, .microsecond=0 на timestamp."""

    async def test_flood_wait_until_preserves_time(self) -> None:
        """flood_wait_until должен содержать текущее UTC время, а не усечённое до часа."""
        repo  = AsyncMock()
        repo.save = AsyncMock(side_effect=lambda obj: obj)
        pool  = UserbotPool(repo=repo)
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        before = datetime.now(timezone.utc)
        await pool.handle_flood_wait(entry, seconds=30)
        after  = datetime.now(timezone.utc)

        ts = entry.model.flood_wait_until
        assert ts is not None
        assert before <= ts <= after, (
            f"flood_wait_until={ts} выходит за рамки [{before}, {after}]. "
            "Возможно, seconds/microseconds были обнулены."
        )

    async def test_flood_wait_until_is_timezone_aware(self) -> None:
        """flood_wait_until должен быть timezone-aware datetime."""
        repo  = AsyncMock()
        repo.save = AsyncMock(side_effect=lambda obj: obj)
        pool  = UserbotPool(repo=repo)
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        await pool.handle_flood_wait(entry, seconds=10)

        ts = entry.model.flood_wait_until
        assert ts is not None
        assert ts.tzinfo is not None, "flood_wait_until должен быть timezone-aware"

    async def test_status_set_to_flood_wait(self) -> None:
        repo  = AsyncMock()
        repo.save = AsyncMock(side_effect=lambda obj: obj)
        pool  = UserbotPool(repo=repo)
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        await pool.handle_flood_wait(entry, seconds=10)

        assert entry.model.status == UserbotStatus.FLOOD_WAIT


# ─── Тест: TOCTOU race condition в acquire ───────────────────────────────────

class TestAcquireRaceCondition:
    """BUG: acquire() проверял is_available до захвата lock — TOCTOU race."""

    async def test_concurrent_acquire_only_one_succeeds(self) -> None:
        """
        При одновременном вызове acquire() двумя корутинами
        только одна должна вернуть True.
        """
        entry = make_entry(id=1, status=UserbotStatus.IDLE, weight=1)

        results = await asyncio.gather(
            entry.acquire(),
            entry.acquire(),
        )

        # Ровно один True
        assert results.count(True)  == 1, (
            f"Ожидался ровно 1 успешный acquire, получено: {results}"
        )
        assert results.count(False) == 1

    async def test_acquire_after_release(self) -> None:
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        assert await entry.acquire() is True
        entry.release()
        assert await entry.acquire() is True

    async def test_acquire_when_busy_returns_false(self) -> None:
        entry = make_entry(id=1, status=UserbotStatus.BUSY)
        assert await entry.acquire() is False

    async def test_acquire_when_over_daily_limit_returns_false(self) -> None:
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        entry.model.requests_today = entry.model.daily_limit
        assert await entry.acquire() is False

    async def test_three_concurrent_acquires(self) -> None:
        """Только один из трёх одновременных acquire должен победить."""
        entry = make_entry(id=1, status=UserbotStatus.IDLE)

        results = await asyncio.gather(
            entry.acquire(),
            entry.acquire(),
            entry.acquire(),
        )

        assert results.count(True) == 1, (
            f"Ожидался ровно 1 успешный acquire из 3, получено: {results}"
        )
