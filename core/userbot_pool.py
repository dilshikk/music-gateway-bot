import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from pyrogram import Client

from infrastructure.database.models import Userbot, UserbotStatus
from infrastructure.database.repositories.userbot_repo import UserbotRepository

logger = logging.getLogger(__name__)


def _as_utc(dt: datetime | None) -> datetime:
    """Приводит дату к aware-UTC, чтобы сортировка не падала на naive/aware."""
    if dt is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class UserbotEntry:
    """Живой объект userbot в памяти."""

    def __init__(self, model: Userbot, client: Client) -> None:
        self.model  = model
        self.client = client
        self._lock  = asyncio.Lock()
        # monotonic-время окончания последнего запроса (для паузы между запросами)
        self.last_finished = 0.0

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
        if not self.is_available:
            return False
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
    Пул userbot-аккаунтов.

    Правила:
      - один аккаунт выполняет один запрос за раз (lock в UserbotEntry);
      - между запросами одного аккаунта пауза ACCOUNT_COOLDOWN секунд;
      - при FloodWait аккаунт отдыхает и сам возвращается в работу.
    """

    # Пауза между запросами одного аккаунта — снижает риск FloodWait и бана
    ACCOUNT_COOLDOWN = 3.0

    def __init__(self, repo: UserbotRepository) -> None:
        self._repo: UserbotRepository = repo
        self._pool: dict[int, UserbotEntry] = {}
        self._bg_tasks: set[asyncio.Task] = set()

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
        for task in self._bg_tasks:
            task.cancel()
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

    async def acquire_userbot(self, userbot_id: int | None = None) -> UserbotEntry | None:
        """
        Занимает свободный аккаунт.
        userbot_id — занять только конкретный аккаунт (например, тот, что делал поиск).
        """
        now = time.monotonic()
        candidates = [
            e for e in self._pool.values()
            if e.is_available
            and now - e.last_finished >= self.ACCOUNT_COOLDOWN
            and (userbot_id is None or e.id == userbot_id)
        ]
        candidates.sort(key=lambda e: (-e.model.weight, _as_utc(e.model.last_used)))

        for entry in candidates:
            if await entry.acquire():
                await self._mark_busy(entry)
                return entry

        return None

    async def release_userbot(self, entry: UserbotEntry) -> None:
        """Освобождает userbot после выполнения запроса."""
        # Если аккаунт отключили из админки во время запроса — не включаем обратно
        if entry.model.status == UserbotStatus.BUSY:
            entry.model.status = UserbotStatus.IDLE
        entry.model.last_used   = datetime.now(timezone.utc)
        entry.model.requests_today += 1
        entry.model.requests_total += 1
        entry.last_finished = time.monotonic()
        entry.release()
        await self._repo.save(entry.model)
        logger.debug("Userbot #%d освобождён", entry.id)

    async def handle_flood_wait(self, entry: UserbotEntry, seconds: int) -> None:
        """Ставит userbot в режим FloodWait с авто-восстановлением."""
        # Статус меняем ДО снятия lock, чтобы аккаунт не успел занять кто-то другой
        entry.model.status = UserbotStatus.FLOOD_WAIT
        entry.model.flood_wait_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        entry.last_finished = time.monotonic()
        entry.release()
        await self._repo.save(entry.model)
        logger.warning("Userbot #%d FloodWait %ds", entry.id, seconds)
        task = asyncio.create_task(self._recover_after_flood(entry, seconds))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    async def _recover_after_flood(self, entry: UserbotEntry, seconds: int) -> None:
        await asyncio.sleep(seconds + 5)
        # Не включаем аккаунт, если его за это время отключили или удалили
        if entry.model.status != UserbotStatus.FLOOD_WAIT:
            return
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
        """Останавливает клиента и УДАЛЯЕТ запись из БД полностью."""
        entry = self._pool.pop(userbot_id, None)
        if entry:
            try:
                await entry.client.stop()
            except Exception:
                pass
        await self._repo.delete(userbot_id)
        logger.info("Userbot #%d удалён из БД", userbot_id)

    async def restart_userbot(self, userbot_id: int) -> bool:
        model = await self._repo.get_by_id(userbot_id)
        if not model:
            return False
        entry = self._pool.pop(userbot_id, None)
        if entry:
            try:
                await entry.client.stop()
            except Exception:
                pass
        return await self._start_userbot(model) is not None

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
