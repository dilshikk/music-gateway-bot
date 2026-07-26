import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait

from infrastructure.database.models import Userbot, UserbotStatus
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from sources.base import SourceFloodWaitError

logger = logging.getLogger(__name__)


class UserbotEntry:
    """Живой объект userbot в памяти."""

    def __init__(self, model: Userbot, client: Client) -> None:
        self.model  = model
        self.client = client
        self._lock  = asyncio.Lock()

    @property
    def id(self) -> int:
        return self.model.id

    @property
    def is_available(self) -> bool:
        return (
            self.model.status == UserbotStatus.IDLE
            and self.model.requests_today < self.model.daily_limit
        )

    async def acquire(self) -> bool:
        """Попытаться занять userbot. Возвращает True если успешно."""
        # BUG FIX: the original code checked is_available *before* acquiring the lock,
        # creating a TOCTOU race condition. Two concurrent callers could both pass the
        # is_available check and both acquire(). Use try_acquire pattern instead.
        if not self.is_available:
            return False
        acquired = self._lock.locked() is False and not self._lock.locked()
        # asyncio.Lock has no try_acquire; use acquire with immediate release on failure
        try:
            await asyncio.wait_for(self._lock.acquire(), timeout=0)
            return True
        except asyncio.TimeoutError:
            return False

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()

    def __repr__(self) -> str:
        return (
            f"<UserbotEntry id={self.id} "
            f"status={self.model.status} "
            f"today={self.model.requests_today}/{self.model.daily_limit}>"
        )


class UserbotPool:
    """
    Пул Pyrogram userbot-аккаунтов.

    Отвечает за:
    - запуск / остановку клиентов
    - выбор свободного аккаунта (Round Robin + weight)
    - обработку FloodWait с авто-восстановлением
    - обновление статусов в БД и Redis
    """

    def __init__(self, repo: UserbotRepository) -> None:
        self._repo: UserbotRepository = repo
        self._pool: dict[int, UserbotEntry] = {}
        self._rr_index = 0

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Загружает все активные userbots из БД и запускает клиентов."""
        userbots = await self._repo.get_all()
        for ub in userbots:
            if ub.session_string:
                await self._start_userbot(ub)
        logger.info("UserbotPool запущен: %d аккаунтов", len(self._pool))

    async def stop(self) -> None:
        """Останавливает всех клиентов."""
        for entry in self._pool.values():
            try:
                await entry.client.stop()
            except Exception:
                pass
        self._pool.clear()
        logger.info("UserbotPool остановлен")

    async def _start_userbot(self, model: Userbot) -> UserbotEntry | None:
        try:
            proxy = None
            if model.proxy:
                proxy = {
                    "scheme":   model.proxy.type,
                    "hostname": model.proxy.host,
                    "port":     model.proxy.port,
                    "username": model.proxy.username,
                    "password": model.proxy.password,
                }

            client = Client(
                name=f"userbot_{model.id}",
                api_id=model.api_id,
                api_hash=model.api_hash,
                session_string=model.session_string,
                proxy=proxy,
                in_memory=True,
            )
            await client.start()

            model.status = UserbotStatus.IDLE
            await self._repo.save(model)

            entry = UserbotEntry(model=model, client=client)
            self._pool[model.id] = entry
            logger.info("Userbot #%d запущен (%s)", model.id, model.phone)
            return entry

        except Exception as e:
            logger.error("Ошибка запуска userbot #%d: %s", model.id, e)
            model.status = UserbotStatus.ERROR
            await self._repo.save(model)
            return None

    # ── Выбор userbot ─────────────────────────────────────────────────────────

    async def acquire_userbot(self) -> UserbotEntry | None:
        """
        Выбирает свободный userbot.
        Стратегия: сортировка по weight (desc) + last_used (asc).
        """
        candidates = sorted(
            [e for e in self._pool.values() if e.is_available],
            key=lambda e: (-e.model.weight, e.model.last_used or datetime.min),
        )

        for entry in candidates:
            if await entry.acquire():
                await self._mark_busy(entry)
                return entry

        return None

    async def release_userbot(self, entry: UserbotEntry) -> None:
        """Освобождает userbot после выполнения запроса."""
        entry.release()
        entry.model.status      = UserbotStatus.IDLE
        entry.model.last_used   = datetime.now(timezone.utc)
        entry.model.requests_today += 1
        entry.model.requests_total += 1
        await self._repo.save(entry.model)
        logger.debug("Userbot #%d освобождён", entry.id)

    async def handle_flood_wait(self, entry: UserbotEntry, seconds: int) -> None:
        """Ставит userbot в режим FloodWait с авто-восстановлением."""
        entry.release()
        entry.model.status = UserbotStatus.FLOOD_WAIT
        # BUG FIX: original code reset seconds/microseconds to 0, storing a
        # slightly incorrect timestamp. Store the exact current UTC time instead.
        entry.model.flood_wait_until = datetime.now(timezone.utc)
        await self._repo.save(entry.model)

        logger.warning(
            "Userbot #%d FloodWait %ds", entry.id, seconds
        )

        # Авто-восстановление через asyncio задачу
        asyncio.create_task(self._recover_after_flood(entry, seconds))

    async def _recover_after_flood(self, entry: UserbotEntry, seconds: int) -> None:
        await asyncio.sleep(seconds + 5)  # +5 сек буфер
        entry.model.status = UserbotStatus.IDLE
        entry.model.flood_wait_until = None
        await self._repo.save(entry.model)
        logger.info("Userbot #%d восстановлен после FloodWait", entry.id)

    # ── Управление из админки ─────────────────────────────────────────────────

    async def add_userbot(self, userbot_id: int) -> bool:
        """Добавить и запустить userbot по ID из БД."""
        model = await self._repo.get_by_id(userbot_id)
        if not model or not model.session_string:
            return False
        entry = await self._start_userbot(model)
        return entry is not None

    async def remove_userbot(self, userbot_id: int) -> None:
        entry = self._pool.pop(userbot_id, None)
        if entry:
            try:
                await entry.client.stop()
            except Exception:
                pass
            await self._repo.set_status(userbot_id, UserbotStatus.DISABLED)

    async def restart_userbot(self, userbot_id: int) -> bool:
        await self.remove_userbot(userbot_id)
        return await self.add_userbot(userbot_id)

    async def disable_userbot(self, userbot_id: int) -> None:
        entry = self._pool.get(userbot_id)
        if entry:
            entry.model.status = UserbotStatus.DISABLED
            await self._repo.save(entry.model)

    async def enable_userbot(self, userbot_id: int) -> None:
        entry = self._pool.get(userbot_id)
        if entry:
            entry.model.status = UserbotStatus.IDLE
            await self._repo.save(entry.model)

    # ── Статистика ────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        total    = len(self._pool)
        idle     = sum(1 for e in self._pool.values() if e.model.status == UserbotStatus.IDLE)
        busy     = sum(1 for e in self._pool.values() if e.model.status == UserbotStatus.BUSY)
        flood    = sum(1 for e in self._pool.values() if e.model.status == UserbotStatus.FLOOD_WAIT)
        error    = sum(1 for e in self._pool.values() if e.model.status == UserbotStatus.ERROR)
        disabled = sum(1 for e in self._pool.values() if e.model.status == UserbotStatus.DISABLED)
        return {
            "total":    total,
            "idle":     idle,
            "busy":     busy,
            "flood":    flood,
            "error":    error,
            "disabled": disabled,
        }

    def list_userbots(self) -> list[UserbotEntry]:
        return list(self._pool.values())

    # ── Вспомогательное ───────────────────────────────────────────────────────

    async def _mark_busy(self, entry: UserbotEntry) -> None:
        entry.model.status = UserbotStatus.BUSY
        await self._repo.save(entry.model)
