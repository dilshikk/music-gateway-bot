"""
Регрессионные тесты для core/userbot_pool.py.

Покрывают исправленные баги:
1. flood_wait_until — это момент ОКОНЧАНИЯ паузы (now + seconds)
2. acquire() имел TOCTOU race condition — два корутина могли оба пройти
3. release не должен включать отключённый аккаунт
4. пауза между запросами одного аккаунта
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

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


def make_pool() -> UserbotPool:
    repo  = AsyncMock()
    repo.save = AsyncMock(side_effect=lambda obj: obj)
    return UserbotPool(repo=repo)


# ─── Тест: flood_wait_until ───────────────────────────────────────────────────────────────

class TestHandleFloodWaitTimestamp:
    async def test_flood_wait_until_is_end_of_pause(self) -> None:
        """flood_wait_until = момент, когда аккаунт снова можно использовать."""
        pool  = make_pool()
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        before = datetime.now(timezone.utc)
        await pool.handle_flood_wait(entry, seconds=30)
        after  = datetime.now(timezone.utc)

        ts = entry.model.flood_wait_until
        assert ts is not None
        assert before + timedelta(seconds=30) <= ts <= after + timedelta(seconds=30)
        await pool.stop()

    async def test_flood_wait_until_is_timezone_aware(self) -> None:
        pool  = make_pool()
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        await pool.handle_flood_wait(entry, seconds=10)

        ts = entry.model.flood_wait_until
        assert ts is not None
        assert ts.tzinfo is not None
        await pool.stop()

    async def test_status_set_to_flood_wait(self) -> None:
        pool  = make_pool()
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()

        await pool.handle_flood_wait(entry, seconds=10)

        assert entry.model.status == UserbotStatus.FLOOD_WAIT
        await pool.stop()


# ─── Тест: статусы и пауза ───────────────────────────────────────────────────────────────

class TestReleaseAndCooldown:
    async def test_release_keeps_disabled(self) -> None:
        """Аккаунт, отключённый во время запроса, не должен стать IDLE."""
        pool  = make_pool()
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry
        await entry.acquire()
        entry.model.status = UserbotStatus.DISABLED

        await pool.release_userbot(entry)

        assert entry.model.status == UserbotStatus.DISABLED

    async def test_cooldown_blocks_immediate_reuse(self) -> None:
        pool  = make_pool()
        entry = make_entry(id=1, status=UserbotStatus.IDLE)
        pool._pool[1] = entry

        first = await pool.acquire_userbot()
        assert first is not None
        await pool.release_userbot(first)

        # Сразу после запроса аккаунт отдыхает
        assert await pool.acquire_userbot() is None

        pool.ACCOUNT_COOLDOWN = 0
        assert await pool.acquire_userbot() is not None

    async def test_acquire_specific_userbot(self) -> None:
        pool = make_pool()
        pool._pool[1] = make_entry(id=1, status=UserbotStatus.IDLE, weight=10)
        pool._pool[2] = make_entry(id=2, status=UserbotStatus.IDLE, weight=1)

        entry = await pool.acquire_userbot(userbot_id=2)
        assert entry is not None
        assert entry.id == 2


# ─── Тест: TOCTOU race condition в acquire ───────────────────────────────────────────────────

class TestAcquireRaceCondition:
    async def test_concurrent_acquire_only_one_succeeds(self) -> None:
        entry = make_entry(id=1, status=UserbotStatus.IDLE, weight=1)

        results = await asyncio.gather(
            entry.acquire(),
            entry.acquire(),
        )

        assert results.count(True)  == 1
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
        entry = make_entry(id=1, status=UserbotStatus.IDLE)

        results = await asyncio.gather(
            entry.acquire(),
            entry.acquire(),
            entry.acquire(),
        )

        assert results.count(True) == 1
