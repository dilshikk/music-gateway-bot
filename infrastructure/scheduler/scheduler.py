import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)


class AppScheduler:
    """
    Центральный планировщик задач.

    Задачи:
        Каждую ночь  00:00  — сброс дневных счётчиков userbots
        Каждый час          — очистка просроченного кэша Redis
        Каждые 5 мин        — проверка и восстановление userbots
        Каждые 30 мин       — обновление статистики источников в БД
        Каждое воскресенье  — архивация старых поисков (> 30 дней)
    """

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler(timezone="UTC")
        self._jobs: list[str] = []

    def register_all(
        self,
        pool,
        cache,
        registry,
        engine,
    ) -> None:
        """Регистрирует все задачи. Вызывается один раз при старте."""

        # 1. Сброс дневных счётчиков — каждую ночь в 00:00 UTC
        self._add(
            func=_reset_daily_counters,
            trigger=CronTrigger(hour=0, minute=0),
            job_id="reset_daily_counters",
            kwargs={"pool": pool},
        )

        # 2. Очистка просроченных ключей Redis — каждый час
        self._add(
            func=_cleanup_expired_cache,
            trigger=IntervalTrigger(hours=1),
            job_id="cleanup_cache",
            kwargs={"cache": cache},
        )

        # 3. Авто-восстановление userbots — каждые 5 минут
        self._add(
            func=_recover_userbots,
            trigger=IntervalTrigger(minutes=5),
            job_id="recover_userbots",
            kwargs={"pool": pool},
        )

        # 4. Обновление статистики источников в БД — каждые 30 минут
        self._add(
            func=_sync_source_stats,
            trigger=IntervalTrigger(minutes=30),
            job_id="sync_source_stats",
            kwargs={"registry": registry, "engine": engine},
        )

        # 5. Архивация старых поисков — каждое воскресенье в 03:00 UTC
        self._add(
            func=_archive_old_searches,
            trigger=CronTrigger(day_of_week="sun", hour=3, minute=0),
            job_id="archive_searches",
            kwargs={"engine": engine},
        )

        # 6. Сброс счётчика ошибок userbots — каждый день в 06:00 UTC
        self._add(
            func=_reset_error_counts,
            trigger=CronTrigger(hour=6, minute=0),
            job_id="reset_error_counts",
            kwargs={"pool": pool},
        )

        logger.info("Scheduler: зарегистрировано %d задач", len(self._jobs))

    def _add(self, func, trigger, job_id: str, kwargs: dict) -> None:
        self._scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            kwargs=kwargs,
            replace_existing=True,
            misfire_grace_time=60,  # если сервер был недоступен — запустить в течение 60с
        )
        self._jobs.append(job_id)

    async def start(self) -> None:
        self._scheduler.start()
        logger.info("Scheduler запущен")

    async def stop(self) -> None:
        self._scheduler.shutdown(wait=False)
        logger.info("Scheduler остановлен")

    def get_jobs_info(self) -> list[dict]:
        jobs = []
        for job in self._scheduler.get_jobs():
            jobs.append({
                "id":       job.id,
                "name":     job.func.__name__,
                "next_run": str(job.next_run_time),
                "trigger":  str(job.trigger),
            })
        return jobs
