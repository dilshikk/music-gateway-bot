"""
Точка входа для отдельного воркер-процесса.

Запуск:
    python -m core.worker               # обычный запуск
    python -m core.worker --load-test   # нагрузочный тест (500 задач)

Что делает этот модуль:
  - Инициализирует все зависимости (Redis, DB, пул ботов, менеджеры)
  - Регистрирует все доступные источники музыки
  - Запускает QueueManager с пулом воркеров
  - Запускает периодические health-check и метрики
  - Корректно завершает работу при SIGTERM/SIGINT
  - Опционально: запускает нагрузочный тест очереди
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from typing import Any

from redis.asyncio import Redis

from config.settings import settings
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchContext, SearchManager
from core.userbot_pool import UserbotPool
from infrastructure.database.repositories.userbot_repo import UserbotRepository
from infrastructure.database.session import async_session_factory
from sources.custom_source import CustomMusicSource
from sources.registry import SourceRegistry
from sources.vk_music_bot import VKMusicBotSource

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


# ─── Создание компонентов ─────────────────────────────────────────────────────

async def create_components() -> tuple[UserbotPool, QueueManager, CacheManager]:
    """
    Собирает все зависимости и возвращает готовые компоненты.

    Порядок инициализации важен:
    Redis → CacheManager → UserbotPool → SourceRegistry → SearchManager → QueueManager
    """
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    cache = CacheManager(redis)

    # Проверяем соединение с Redis перед стартом
    if not await cache.ping():
        raise RuntimeError(
            "Не удалось подключиться к Redis. "
            f"Проверь REDIS_URL: {settings.redis_url}"
        )
    logger.info("Redis: подключено (%s)", settings.redis_url)

    async with async_session_factory() as session:
        repo = UserbotRepository(session)
        pool = UserbotPool(repo)

    registry = SourceRegistry()

    # ── Регистрируем источники ────────────────────────────────────────────────
    #
    # VK Music Bot (через Pyrogram userbot, высший приоритет)
    # client=None — реальный клиент подставляется из пула при каждом запросе
    registry.register(
        VKMusicBotSource(
            client=None,   # type: ignore[arg-type]
            priority=10,
        )
    )

    # Custom HTTP API-источник (средний приоритет, fallback)
    # Включается только если задан CUSTOM_SOURCE_BASE_URL
    custom_url = os.getenv("CUSTOM_SOURCE_BASE_URL", "")
    if custom_url:
        registry.register(
            CustomMusicSource(
                base_url=custom_url,
                priority=5,
                timeout=15,
                enabled=True,
            )
        )
        logger.info("CustomMusicSource зарегистрирован: %s", custom_url)
    else:
        logger.info(
            "CustomMusicSource пропущен (CUSTOM_SOURCE_BASE_URL не задан). "
            "Задай переменную окружения для активации."
        )

    logger.info("Зарегистрированы источники: %r", registry)

    search = SearchManager(pool=pool, registry=registry, cache=cache)
    queue  = QueueManager(search_manager=search, cache=cache)

    return pool, queue, cache


# ─── Фоновые задачи ───────────────────────────────────────────────────────────

async def _health_check_loop(
    queue: QueueManager,
    cache: CacheManager,
    interval: int = 30,
) -> None:
    """
    Периодически проверяет состояние системы и логирует метрики.
    Запускается как отдельная asyncio-задача.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            stats   = queue.get_stats()
            redis_ok = await cache.ping()
            logger.info(
                "Health | redis=%s queue_size=%d workers=%d "
                "pending=%d processing=%d done=%d failed=%d",
                "OK" if redis_ok else "FAIL",
                stats.get("queue_size", 0),
                stats.get("workers", 0),
                stats.get("pending", 0),
                stats.get("processing", 0),
                stats.get("done", 0),
                stats.get("failed", 0),
            )
        except Exception as e:
            logger.error("Health check error: %s", e)


async def _metrics_loop(queue: QueueManager, interval: int = 60) -> None:
    """
    Выводит расширенные метрики раз в минуту.
    В production замени на отправку в Prometheus / Grafana.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            stats = queue.get_stats()
            logger.info("=== Метрики воркера ===")
            for key, val in stats.items():
                logger.info("  %s: %s", key, val)
        except Exception as e:
            logger.error("Metrics error: %s", e)


# ─── Нагрузочный тест очереди ─────────────────────────────────────────────────

async def run_load_test(queue: QueueManager, n_tasks: int = 500) -> None:
    """
    Нагрузочный тест: отправляет n_tasks задач в очередь и замеряет throughput.

    Метрики:
      - Общее время выполнения всех задач
      - Throughput (задач/сек)
      - Процент успешных задач
      - Среднее, минимальное, максимальное время выполнения
      - Распределение по квантилям (p50, p95, p99)

    Запуск:
        python -m core.worker --load-test
        python -m core.worker --load-test --tasks 1000
    """
    logger.info("=" * 60)
    logger.info("Нагрузочный тест: %d задач, %d воркеров", n_tasks, queue.WORKERS_COUNT)
    logger.info("=" * 60)

    queries = [
        "Imagine Dragons Believer",
        "Arctic Monkeys Do I Wanna Know",
        "Queen Bohemian Rhapsody",
        "The Weeknd Blinding Lights",
        "Billie Eilish Bad Guy",
        "Ed Sheeran Shape of You",
        "Post Malone Rockstar",
        "Drake God's Plan",
        "Kendrick Lamar HUMBLE",
        "Travis Scott SICKO MODE",
    ]

    tasks_submitted: list[Any] = []
    submit_errors = 0
    wall_start = time.monotonic()

    # ── Фаза 1: Отправка задач ────────────────────────────────────────────────
    logger.info("Фаза 1: отправка %d задач в очередь...", n_tasks)
    submit_start = time.monotonic()

    for i in range(n_tasks):
        query = queries[i % len(queries)]
        ctx = SearchContext(
            query=query,
            user_id=100_000 + (i % 50),  # 50 уникальных пользователей
            page=1,
        )
        is_premium = (i % 10 == 0)  # 10% Premium
        try:
            task = await queue.enqueue(ctx, is_premium=is_premium)
            tasks_submitted.append(task)
        except OverflowError:
            # Очередь переполнена — это ожидаемо при n_tasks > MAX_QUEUE_SIZE
            submit_errors += 1
        except PermissionError:
            # Rate limit сработал
            submit_errors += 1

    submit_time = time.monotonic() - submit_start
    logger.info(
        "Отправлено: %d задач за %.2fs (%.0f задач/сек), отклонено: %d",
        len(tasks_submitted),
        submit_time,
        len(tasks_submitted) / max(submit_time, 0.001),
        submit_errors,
    )

    # ── Фаза 2: Ожидание результатов ─────────────────────────────────────────
    logger.info("Фаза 2: ожидание результатов (таймаут 60с)...")
    process_start = time.monotonic()

    latencies: list[float] = []
    success_count = 0
    failed_count  = 0
    timeout_count = 0

    async def _wait_one(task: Any, idx: int) -> tuple[bool, float, str]:
        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                asyncio.shield(task._future),
                timeout=60.0,
            )
            return True, time.monotonic() - t0, "ok"
        except asyncio.TimeoutError:
            return False, time.monotonic() - t0, "timeout"
        except Exception as e:
            return False, time.monotonic() - t0, f"error:{e}"

    # Запускаем ожидание всех задач параллельно
    results = await asyncio.gather(
        *[_wait_one(t, i) for i, t in enumerate(tasks_submitted)],
        return_exceptions=False,
    )

    process_time = time.monotonic() - process_start
    wall_time    = time.monotonic() - wall_start

    for ok, latency, reason in results:
        latencies.append(latency)
        if ok:
            success_count += 1
        elif "timeout" in reason:
            timeout_count += 1
        else:
            failed_count += 1

    # ── Фаза 3: Вывод статистики ──────────────────────────────────────────────
    total = len(tasks_submitted)
    if total == 0:
        logger.warning("Нет обработанных задач. Проверь логи воркера.")
        return

    latencies.sort()
    p50  = latencies[int(total * 0.50)]
    p95  = latencies[int(total * 0.95)]
    p99  = latencies[min(int(total * 0.99), total - 1)]
    avg  = sum(latencies) / total
    minl = latencies[0]
    maxl = latencies[-1]

    logger.info("=" * 60)
    logger.info("РЕЗУЛЬТАТЫ НАГРУЗОЧНОГО ТЕСТА")
    logger.info("=" * 60)
    logger.info("Задач отправлено:     %d", total)
    logger.info("Задач отклонено:      %d (queue full / rate limit)", submit_errors)
    logger.info("Успешно:              %d (%.1f%%)", success_count, 100 * success_count / total)
    logger.info("Ошибки:               %d (%.1f%%)", failed_count,  100 * failed_count  / total)
    logger.info("Таймауты:             %d (%.1f%%)", timeout_count, 100 * timeout_count / total)
    logger.info("-" * 40)
    logger.info("Общее время (wall):   %.2fs", wall_time)
    logger.info("Время обработки:      %.2fs", process_time)
    logger.info("Throughput:           %.1f задач/сек", total / max(wall_time, 0.001))
    logger.info("-" * 40)
    logger.info("Задержка (latency):")
    logger.info("  min:  %.3fs", minl)
    logger.info("  avg:  %.3fs", avg)
    logger.info("  p50:  %.3fs", p50)
    logger.info("  p95:  %.3fs", p95)
    logger.info("  p99:  %.3fs", p99)
    logger.info("  max:  %.3fs", maxl)
    logger.info("=" * 60)

    # Итоговый вердикт
    success_rate = success_count / total
    if success_rate >= 0.95:
        logger.info("✅ ТЕСТ ПРОЙДЕН (%.1f%% успешных задач)", 100 * success_rate)
    elif success_rate >= 0.80:
        logger.warning("⚠️  ТЕСТ ЧАСТИЧНО ПРОЙДЕН (%.1f%% успешных)", 100 * success_rate)
    else:
        logger.error("❌ ТЕСТ ПРОВАЛЕН (%.1f%% успешных, ожидалось ≥80%%)", 100 * success_rate)
        sys.exit(1)


# ─── Основной цикл ────────────────────────────────────────────────────────────

async def main(load_test: bool = False, load_test_tasks: int = 500) -> None:
    logger.info("Запуск воркера...")

    pool, queue, cache = await create_components()

    # Graceful shutdown через asyncio.Event
    shutdown_event = asyncio.Event()

    def _handle_signal(sig: signal.Signals) -> None:
        logger.info("Получен сигнал %s, начинаем остановку...", sig.name)
        shutdown_event.set()

    # Регистрируем обработчики SIGTERM и SIGINT
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _handle_signal, sig)
        except NotImplementedError:
            # Windows не поддерживает add_signal_handler для SIGTERM
            signal.signal(sig, lambda s, f: shutdown_event.set())

    # Стартуем компоненты
    await pool.start()
    await queue.start()
    logger.info("Воркер готов (workers=%d)", queue.WORKERS_COUNT)

    # Запускаем фоновые задачи
    background_tasks = [
        asyncio.create_task(_health_check_loop(queue, cache, interval=30)),
        asyncio.create_task(_metrics_loop(queue, interval=60)),
    ]

    try:
        if load_test:
            # Режим нагрузочного теста — запускаем и выходим
            logger.info("Запускаем нагрузочный тест (%d задач)...", load_test_tasks)
            # Небольшая пауза чтобы воркеры успели стартовать
            await asyncio.sleep(0.1)
            await run_load_test(queue, n_tasks=load_test_tasks)
            shutdown_event.set()
        else:
            # Обычный режим — ждём сигнала остановки
            logger.info("Воркер работает. Для остановки: Ctrl+C или SIGTERM")
            await shutdown_event.wait()

    finally:
        logger.info("Останавливаем фоновые задачи...")
        for t in background_tasks:
            t.cancel()
        await asyncio.gather(*background_tasks, return_exceptions=True)

        logger.info("Останавливаем QueueManager и UserbotPool...")
        await queue.stop()
        await pool.stop()
        logger.info("Воркер остановлен. До свидания!")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Music Gateway Bot — Worker Process",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры:
  python -m core.worker                        # обычный запуск
  python -m core.worker --load-test            # нагрузочный тест (500 задач)
  python -m core.worker --load-test --tasks 1000  # нагрузочный тест (1000 задач)
        """,
    )
    parser.add_argument(
        "--load-test",
        action="store_true",
        help="Запустить нагрузочный тест очереди и выйти",
    )
    parser.add_argument(
        "--tasks",
        type=int,
        default=500,
        metavar="N",
        help="Количество задач для нагрузочного теста (default: 500)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    asyncio.run(
        main(
            load_test=args.load_test,
            load_test_tasks=args.tasks,
        )
    )
