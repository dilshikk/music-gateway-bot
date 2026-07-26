"""
Watchdog — перезапускает зависшие компоненты.
Запускается отдельной задачей.
"""
import asyncio
import logging

from core.userbot_pool import UserbotPool
from infrastructure.database.models import UserbotStatus

logger = logging.getLogger(__name__)

WATCHDOG_INTERVAL = 60  # секунды


class Watchdog:
    def __init__(self, pool: UserbotPool) -> None:
        self._pool = pool
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())
        logger.info("Watchdog запущен")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            await self._check_stuck_userbots()

    async def _check_stuck_userbots(self) -> None:
        """
        Если userbot застрял в статусе BUSY дольше 5 минут —
        считаем его зависшим и переводим в IDLE.
        """
        import time
        now = time.time()

        for entry in self._pool.list_userbots():
            if entry.model.status != UserbotStatus.BUSY:
                continue

            last_used = entry.model.last_used
            if not last_used:
                continue

            busy_seconds = now - last_used.timestamp()
            if busy_seconds > 300:  # 5 минут
                logger.warning(
                    "Watchdog: userbot #%d завис (busy %ds), сбрасываем",
                    entry.id, int(busy_seconds),
                )
                entry.release()
                entry.model.status = UserbotStatus.IDLE
                from infrastructure.database.session import async_session_factory
                async with async_session_factory() as session:
                    from infrastructure.database.repositories.userbot_repo import UserbotRepository
                    await UserbotRepository(session).save(entry.model)
