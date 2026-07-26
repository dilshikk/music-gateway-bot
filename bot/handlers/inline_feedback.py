"""
Обработчик ChosenInlineResult с feedback loop.

Telegram присылает chosen_inline_result только если у бота
включён inline feedback (BotFather → /setinlinefeedback → 100%)
"""
import logging

from aiogram import Router
from aiogram.types import ChosenInlineResult

from core.cache_manager import CacheManager

logger = logging.getLogger(__name__)

router = Router(name="inline_feedback")


@router.chosen_inline_result()
async def on_chosen_result(
    result: ChosenInlineResult,
    cache: CacheManager,
) -> None:
    tg_id = result.from_user.id
    query = result.query.strip()

    if not query:
        return

    # Аналитика: обновляем счётчик популярных запросов
    await cache.increment_popular(query)
    await cache.add_to_history(tg_id, query)

    logger.info(
        "[InlineFeedback] user=%d chose result=%r for query=%r",
        tg_id, result.result_id, query,
    )
