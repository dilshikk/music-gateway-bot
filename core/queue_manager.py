import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from core.cache_manager import CacheManager
from core.search_manager import SearchContext, SearchManager
from sources.base import AudioFile, SearchResult

logger = logging.getLogger(__name__)


class TaskStatus(str, Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    DONE       = "done"
    FAILED     = "failed"
    TIMEOUT    = "timeout"


class TaskPriority(int, Enum):
    HIGH   = 10  # Premium пользователи
    NORMAL = 5   # Обычные


@dataclass
class SearchTask:
    task_id:     str
    ctx:         SearchContext
    priority:    TaskPriority
    created_at:  float = field(default_factory=time.monotonic)
    status:      TaskStatus = TaskStatus.PENDING
    result:      SearchResult | None = None
    audio:       AudioFile | None = None
    error:       str | None = None

    # Future для ожидания результата
    _future: asyncio.Future = field(
        default_factory=lambda: asyncio.get_event_loop().create_future(),
        repr=False,
    )

    def resolve(self, result: SearchResult) -> None:
        self.result = result
        self.status = TaskStatus.DONE
        if not self._future.done():
            self._future.set_result(result)

    def reject(self, error: str) -> None:
        self.error  = error
        self.status = TaskStatus.FAILED
        if not self._future.done():
            self._future.set_exception(Exception(error))

    def expire(self) -> None:
        self.status = TaskStatus.TIMEOUT
        if not self._future.done():
            self._future.set_exception(
                asyncio.TimeoutError("Превышено время ожидания в очереди")
            )


class QueueManager:
    """
    Приоритетная очередь поисковых запросов.

    - Premium пользователи: HIGH priority → обрабатываются первыми
    - Обычные: NORMAL priority
    - Максимальное время ожидания: MAX_WAIT_SECONDS
    - Максимальный размер очереди: MAX_QUEUE_SIZE
    """

    MAX_WAIT_SECONDS = 120
    MAX_QUEUE_SIZE   = 500
    WORKERS_COUNT    = 10  # параллельных воркеров

    def __init__(
        self,
        search_manager: SearchManager,
        cache: CacheManager,
    ) -> None:
        self._search   = search_manager
        self._cache    = cache
        self._queue: asyncio.PriorityQueue = asyncio.PriorityQueue(
            maxsize=self.MAX_QUEUE_SIZE
        )
        self._tasks: dict[str, SearchTask] = {}
        self._workers: list[asyncio.Task] = []
        self._running = False

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i))
            for i in range(self.WORKERS_COUNT)
        ]
        logger.info("QueueManager запущен: %d воркеров", self.WORKERS_COUNT)

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("QueueManager остановлен")

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def enqueue(
        self,
        ctx: SearchContext,
        is_premium: bool = False,
    ) -> SearchTask:
        """
        Добавить запрос в очередь.
        Возвращает SearchTask — можно ждать результата через task._future.
        """
        if self._queue.full():
            raise OverflowError(
                f"Очередь переполнена ({self.MAX_QUEUE_SIZE} запросов). "
                "Попробуйте позже."
            )

        # Проверяем rate limit
        allowed, retry_after = await self._cache.check_rate_limit(ctx.user_id)
        if not allowed:
            raise PermissionError(
                f"Слишком много запросов. Подождите {retry_after} сек."
            )

        priority = TaskPriority.HIGH if is_premium else TaskPriority.NORMAL
        task = SearchTask(
            task_id=str(uuid.uuid4()),
            ctx=ctx,
            priority=priority,
        )
        self._tasks[task.task_id] = task

        # PriorityQueue: меньше = выше приоритет, поэтому инвертируем
        await self._queue.put((-priority.value, time.monotonic(), task))

        # Таймер для автоматического истечения задачи
        asyncio.create_task(self._expire_task(task))

        logger.debug(
            "Задача %s добавлена в очередь (priority=%s, size=%d)",
            task.task_id[:8], priority.name, self._queue.qsize(),
        )
        return task

    async def wait_for_result(
        self,
        task: SearchTask,
        timeout: float | None = None,
    ) -> SearchResult:
        """Ждёт результата задачи."""
        return await asyncio.wait_for(
            asyncio.shield(task._future),
            timeout=timeout or self.MAX_WAIT_SECONDS,
        )

    def get_queue_size(self) -> int:
        return self._queue.qsize()

    def get_position(self, task_id: str) -> int | None:
        """Приблизительная позиция в очереди (для уведомления пользователя)."""
        task = self._tasks.get(task_id)
        if not task or task.status != TaskStatus.PENDING:
            return None
        # Считаем задачи с более высоким приоритетом и более ранним временем
        position = sum(
            1 for t in self._tasks.values()
            if t.status == TaskStatus.PENDING
            and (t.priority.value > task.priority.value
                 or (t.priority == task.priority and t.created_at < task.created_at))
        )
        return position + 1

    # ── Воркер ────────────────────────────────────────────────────────────────

    async def _worker(self, worker_id: int) -> None:
        logger.debug("Воркер #%d запущен", worker_id)
        while self._running:
            try:
                _, _, task = await asyncio.wait_for(
                    self._queue.get(), timeout=1.0
                )
            except asyncio.TimeoutError:
                continue

            if task.status == TaskStatus.TIMEOUT:
                self._queue.task_done()
                continue

            task.status = TaskStatus.PROCESSING
            logger.debug("Воркер #%d обрабатывает %s", worker_id, task.task_id[:8])

            try:
                result = await self._search.search(task.ctx)
                task.resolve(result)

            except Exception as e:
                logger.error("Ошибка обработки задачи %s: %s", task.task_id[:8], e)
                task.reject(str(e))

            finally:
                self._queue.task_done()
                self._tasks.pop(task.task_id, None)

    async def _expire_task(self, task: SearchTask) -> None:
        """Истекает задачу если она не обработана за MAX_WAIT_SECONDS."""
        await asyncio.sleep(self.MAX_WAIT_SECONDS)
        if task.status == TaskStatus.PENDING:
            task.expire()
            self._tasks.pop(task.task_id, None)
            logger.warning("Задача %s истекла по таймауту", task.task_id[:8])

    # ── Статистика ────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        statuses = {}
        for task in self._tasks.values():
            statuses[task.status.value] = statuses.get(task.status.value, 0) + 1
        return {
            "queue_size":  self._queue.qsize(),
            "tasks_total": len(self._tasks),
            "workers":     self.WORKERS_COUNT,
            **statuses,
        }
