import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from bot.keyboards.search import (
    build_downloading_keyboard,
    build_search_results_keyboard,
)
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchContext, SearchManager
from infrastructure.database.models import User
from sources.base import SearchResult

logger = logging.getLogger(__name__)
router = Router(name="search")

# task_id → SearchResult (для пагинации и скачивания).
# Ограничен по размеру: раньше словарь рос бесконечно и съедал память.
RESULTS_CACHE_MAX = 2000
_results_cache: OrderedDict[str, SearchResult] = OrderedDict()


def _remember_result(task_id: str, result: SearchResult) -> None:
    _results_cache[task_id] = result
    _results_cache.move_to_end(task_id)
    while len(_results_cache) > RESULTS_CACHE_MAX:
        _results_cache.popitem(last=False)


async def _safe_edit_reply_markup(callback: CallbackQuery, keyboard) -> None:
    """edit_reply_markup, игнорируя ошибку 'message is not modified'."""
    try:
        await callback.message.edit_reply_markup(reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.debug("[safe_edit] клавиатура не изменилась, пропускаем")
        else:
            raise


@router.message(F.text & ~F.text.startswith("/"))
async def handle_search_query(
    message: Message,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    query = message.text.strip()
    if not query or len(query) < 2:
        await message.answer(_("search-too-short"))
        return

    wait_msg = await message.answer(_("search-processing"))

    ctx = SearchContext(query=query, user_id=user.id, page=1)

    try:
        # Лимит уже проверен в RateLimitMiddleware — не списываем второй раз
        task = await queue.enqueue(ctx, is_premium=user.premium, check_rate_limit=False)
        pos = queue.get_position(task.task_id)

        if pos and pos > 1:
            await wait_msg.edit_text(
                _("search-queue-position", position=pos, query=query)
            )

        result = await queue.wait_for_result(task)

    except PermissionError as e:
        await wait_msg.edit_text(f"⏳ {e}")
        return
    except asyncio.TimeoutError:
        await wait_msg.edit_text(_("search-timeout"))
        return
    except OverflowError:
        await wait_msg.edit_text(_("search-queue-full"))
        return
    except Exception as e:
        logger.error("Ошибка поиска для %d: %s", user.telegram_id, e)
        await wait_msg.edit_text(_("search-error"))
        return

    if not result.tracks:
        await wait_msg.edit_text(_("search-not-found", query=query))
        return

    _remember_result(task.task_id, result)

    keyboard = build_search_results_keyboard(result, task.task_id)
    await wait_msg.edit_text(
        _("search-results-header", query=query, total=result.total),
        reply_markup=keyboard,
    )


@router.callback_query(F.data.startswith("dl:"))
async def handle_download(
    callback: CallbackQuery,
    user: User,
    search_manager: SearchManager,
    cache: CacheManager,
    _: Callable,  # Имя обязательно '_' — aiogram инжектирует переводчик именно так
) -> None:
    _dl_prefix, task_id, track_idx_str = callback.data.split(":", 2)
    track_idx = int(track_idx_str)

    result = _results_cache.get(task_id)
    if not result or track_idx >= len(result.tracks):
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    track = result.tracks[track_idx]

    await _safe_edit_reply_markup(callback, build_downloading_keyboard())
    await callback.answer()

    try:
        # target_chat_id — аккаунт отправит аудио боту с пометкой,
        # relay-хендлер бота перешлёт его пользователю.
        audio = await search_manager.get_audio(
            track,
            user.id,
            target_chat_id=callback.from_user.id,
        )

        # Источник без relay (например HTTP API) — отправляем сами
        file_ref = audio.telegram_file_id or audio.file_path
        if not audio.already_sent and file_ref and isinstance(callback.message, Message):
            await callback.message.answer_audio(
                audio=file_ref,
                title=audio.title or None,
                performer=audio.artist or None,
                duration=audio.duration or None,
            )

        keyboard = build_search_results_keyboard(result, task_id)
        await _safe_edit_reply_markup(callback, keyboard)

        from infrastructure.database.repositories.user_repo import UserRepository
        from infrastructure.database.session import async_session_factory
        async with async_session_factory() as session:
            await UserRepository(session).increment_requests(user.id)

    except Exception as e:
        logger.error("Ошибка скачивания трека: %s", e)
        keyboard = build_search_results_keyboard(result, task_id)
        await _safe_edit_reply_markup(callback, keyboard)
        await callback.answer(_("download-error"), show_alert=True)


@router.callback_query(F.data.startswith("page:"))
async def handle_pagination(
    callback: CallbackQuery,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    _page_prefix, task_id, page_str = callback.data.split(":", 2)
    page = int(page_str)

    old_result = _results_cache.get(task_id)
    if not old_result:
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    await callback.answer(_("search-processing"))

    ctx = SearchContext(query=old_result.query, user_id=user.id, page=page)

    try:
        # Листание страниц не списывает лимит поисков
        task = await queue.enqueue(ctx, is_premium=user.premium, check_rate_limit=False)
        result = await queue.wait_for_result(task)
        _remember_result(task_id, result)

        keyboard = build_search_results_keyboard(result, task_id)
        await _safe_edit_reply_markup(callback, keyboard)

    except Exception as e:
        logger.error("Ошибка пагинации: %s", e)
        await callback.answer(_("search-error"), show_alert=True)


@router.callback_query(F.data == "close")
async def handle_close(callback: CallbackQuery) -> None:
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer()
