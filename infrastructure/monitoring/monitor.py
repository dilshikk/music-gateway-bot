import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum

import psutil
from aiogram import Bot
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from config.settings import settings
from core.userbot_pool import UserbotPool
from sources.registry import SourceRegistry

logger = logging.getLogger(__name__)


class ServiceStatus(str, Enum):
    OK      = "ok"
    WARN    = "warn"
    ERROR   = "error"
    UNKNOWN = "unknown"


@dataclass
class ServiceCheck:
    name:       str
    status:     ServiceStatus
    message:    str = ""
    latency_ms: float = 0.0
    checked_at: float = field(default_factory=time.time)


@dataclass
class SystemSnapshot:
    cpu_percent:    float
    ram_percent:    float
    ram_used_mb:    int
    disk_percent:   float
    checks:         list[ServiceCheck] = field(default_factory=list)
    snapshot_at:    float = field(default_factory=time.time)

    @property
    def has_errors(self) -> bool:
        return any(c.status == ServiceStatus.ERROR for c in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(c.status == ServiceStatus.WARN for c in self.checks)


class MonitoringService:
    """
    Запускается каждые 30 секунд.
    Проверяет все компоненты системы и оповещает админов при проблемах.

    Пороги предупреждений:
        CPU       > 80%  → WARN,  > 95%  → ERROR
        RAM       > 85%  → WARN,  > 95%  → ERROR
        Очередь   > 50   → WARN,  > 200  → ERROR
        Ошибки    > 10   за 5 мин → WARN
    """

    INTERVAL_SECONDS      = 30
    CPU_WARN_THRESHOLD    = 80.0
    CPU_ERROR_THRESHOLD   = 95.0
    RAM_WARN_THRESHOLD    = 85.0
    RAM_ERROR_THRESHOLD   = 95.0
    QUEUE_WARN_THRESHOLD  = 50
    QUEUE_ERROR_THRESHOLD = 200

    def __init__(
        self,
        bot:      Bot,
        redis:    Redis,
        engine:   AsyncEngine,
        pool:     UserbotPool,
        registry: SourceRegistry,
    ) -> None:
        self._bot      = bot
        self._redis    = redis
        self._engine   = engine
        self._pool     = pool
        self._registry = registry
        self._task:    asyncio.Task | None = None

        # Последний снимок — доступен через API
        self.last_snapshot: SystemSnapshot | None = None

        # Состояния для де-дупликации уведомлений (не спамим каждые 30с)
        self._alerted: set[str] = set()

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())
        logger.info("MonitoringService запущен (интервал %ds)", self.INTERVAL_SECONDS)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("MonitoringService остановлен")

    # ── Основной цикл ─────────────────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while True:
            try:
                snapshot = await self._collect()
                self.last_snapshot = snapshot
                await self._evaluate_and_notify(snapshot)
            except Exception as e:
                logger.error("Ошибка мониторинга: %s", e)
            await asyncio.sleep(self.INTERVAL_SECONDS)

    # ── Сбор данных ───────────────────────────────────────────────────────────

    async def _collect(self) -> SystemSnapshot:
        checks = await asyncio.gather(
            self._check_bot_api(),
            self._check_redis(),
            self._check_postgres(),
            self._check_userbots(),
            self._check_sources(),
            self._check_internet(),
            return_exceptions=False,
        )

        cpu   = psutil.cpu_percent(interval=0.1)
        ram   = psutil.virtual_memory()
        disk  = psutil.disk_usage("/")

        return SystemSnapshot(
            cpu_percent  = cpu,
            ram_percent  = ram.percent,
            ram_used_mb  = ram.used // 1024 // 1024,
            disk_percent = disk.percent,
            checks       = list(checks),
        )

    async def _check_bot_api(self) -> ServiceCheck:
        t = time.monotonic()
        try:
            me = await self._bot.get_me()
            return ServiceCheck(
                name       = "Bot API",
                status     = ServiceStatus.OK,
                message    = f"@{me.username}",
                latency_ms = (time.monotonic() - t) * 1000,
            )
        except Exception as e:
            return ServiceCheck(
                name    = "Bot API",
                status  = ServiceStatus.ERROR,
                message = str(e),
            )

    async def _check_redis(self) -> ServiceCheck:
        t = time.monotonic()
        try:
            await self._redis.ping()
            info  = await self._redis.info("memory")
            used  = info.get("used_memory_human", "?")
            return ServiceCheck(
                name       = "Redis",
                status     = ServiceStatus.OK,
                message    = f"memory: {used}",
                latency_ms = (time.monotonic() - t) * 1000,
            )
        except Exception as e:
            return ServiceCheck(
                name    = "Redis",
                status  = ServiceStatus.ERROR,
                message = str(e),
            )

    async def _check_postgres(self) -> ServiceCheck:
        t = time.monotonic()
        try:
            async with self._engine.connect() as conn:
                result = await conn.execute(text("SELECT count(*) FROM pg_stat_activity"))
                connections = result.scalar()
            return ServiceCheck(
                name       = "PostgreSQL",
                status     = ServiceStatus.OK,
                message    = f"connections: {connections}",
                latency_ms = (time.monotonic() - t) * 1000,
            )
        except Exception as e:
            return ServiceCheck(
                name    = "PostgreSQL",
                status  = ServiceStatus.ERROR,
                message = str(e),
            )

    async def _check_userbots(self) -> ServiceCheck:
        stats = self._pool.get_stats()
        total = stats["total"]
        idle  = stats["idle"]
        flood = stats["flood"]
        error = stats["error"]

        if total == 0:
            return ServiceCheck(
                name    = "Userbots",
                status  = ServiceStatus.ERROR,
                message = "Нет ни одного userbot",
            )
        if error > 0 or idle == 0:
            return ServiceCheck(
                name    = "Userbots",
                status  = ServiceStatus.WARN,
                message = f"idle={idle}, flood={flood}, error={error}",
            )
        return ServiceCheck(
            name    = "Userbots",
            status  = ServiceStatus.OK,
            message = f"idle={idle}/{total}, flood={flood}",
        )

    async def _check_sources(self) -> ServiceCheck:
        available = self._registry.get_available()
        if not available:
            return ServiceCheck(
                name    = "Sources",
                status  = ServiceStatus.ERROR,
                message = "Нет активных источников",
            )

        errors = []
        for source in available:
            try:
                ok = await asyncio.wait_for(source.health_check(), timeout=10)
                if not ok:
                    errors.append(source.name)
            except Exception:
                errors.append(source.name)

        if errors:
            return ServiceCheck(
                name    = "Sources",
                status  = ServiceStatus.WARN,
                message = f"Недоступны: {', '.join(errors)}",
            )
        return ServiceCheck(
            name    = "Sources",
            status  = ServiceStatus.OK,
            message = f"Активно: {len(available)}",
        )

    async def _check_internet(self) -> ServiceCheck:
        import aiohttp
        t = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://api.telegram.org",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    return ServiceCheck(
                        name       = "Internet",
                        status     = ServiceStatus.OK,
                        message    = f"HTTP {resp.status}",
                        latency_ms = (time.monotonic() - t) * 1000,
                    )
        except Exception as e:
            return ServiceCheck(
                name    = "Internet",
                status  = ServiceStatus.ERROR,
                message = str(e),
            )

    # ── Анализ и уведомления ──────────────────────────────────────────────────

    async def _evaluate_and_notify(self, snap: SystemSnapshot) -> None:
        alerts: list[str] = []

        # CPU
        cpu_key = "cpu_high"
        if snap.cpu_percent >= self.CPU_ERROR_THRESHOLD:
            alerts.append(f"🔴 CPU критический: <b>{snap.cpu_percent}%</b>")
            self._alerted.discard(cpu_key)  # всегда шлём при ERROR
        elif snap.cpu_percent >= self.CPU_WARN_THRESHOLD:
            if cpu_key not in self._alerted:
                alerts.append(f"🟡 CPU высокий: <b>{snap.cpu_percent}%</b>")
                self._alerted.add(cpu_key)
        else:
            self._alerted.discard(cpu_key)

        # RAM
        ram_key = "ram_high"
        if snap.ram_percent >= self.RAM_ERROR_THRESHOLD:
            alerts.append(f"🔴 RAM критическая: <b>{snap.ram_percent}%</b> ({snap.ram_used_mb} MB)")
            self._alerted.discard(ram_key)
        elif snap.ram_percent >= self.RAM_WARN_THRESHOLD:
            if ram_key not in self._alerted:
                alerts.append(f"🟡 RAM высокая: <b>{snap.ram_percent}%</b>")
                self._alerted.add(ram_key)
        else:
            self._alerted.discard(ram_key)

        # Сервисы
        for check in snap.checks:
            key = f"service_{check.name}"
            if check.status == ServiceStatus.ERROR:
                alerts.append(f"🔴 <b>{check.name}</b>: {check.message}")
                self._alerted.discard(key)
            elif check.status == ServiceStatus.WARN:
                if key not in self._alerted:
                    alerts.append(f"🟡 <b>{check.name}</b>: {check.message}")
                    self._alerted.add(key)
            else:
                self._alerted.discard(key)

        if alerts:
            await self._send_alert(alerts)

    async def _send_alert(self, alerts: list[str]) -> None:
        text = (
            "⚠️ <b>Мониторинг — предупреждение</b>\n\n"
            + "\n".join(alerts)
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await self._bot.send_message(admin_id, text)
            except Exception as e:
                logger.error("Не удалось отправить алерт админу %d: %s", admin_id, e)

    # ── Публичный снимок (для API) ────────────────────────────────────────────

    def get_snapshot(self) -> SystemSnapshot | None:
        return self.last_snapshot
