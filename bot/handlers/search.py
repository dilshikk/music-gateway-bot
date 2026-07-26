import asyncio
import logging
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
from sources.base import SearchResult, Track

logger = logging.getLogger(__name__)
router = Router(name="search")

# task_id → SearchResult (для пагинации и скачивания)
_results_cache: dict[str, SearchResult] = {}


async def _safe_edit_markup(msg, reply_markup) -> None:
    """
    Безопасно обновляет reply_markup — игнорирует ошибку
    'message is not modified' при двойном нажатии.
    """
    try:
        await msg.edit_reply_markup(reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
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
        task = await queue.enqueue(ctx, is_premium=user.premium)
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

    _results_cache[task.task_id] = result

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
    _: Callable,
) -> None:
    parts     = callback.data.split(":", 2)
    task_id   = parts[1]
    track_idx = int(parts[2])

    result = _results_cache.get(task_id)
    if not result or track_idx >= len(result.tracks):
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    track = result.tracks[track_idx]

    await _safe_edit_markup(callback.message, build_downloading_keyboard())
    await callback.answer()

    try:
        # Userbot доставляет аудио напрямую в чат пользователя (copy_message).
        # file_id из Pyrogram-сессии невалиден для главного бота.
        target_chat_id = callback.message.chat.id
        audio = await search_manager.get_audio(
            track, user.id, target_chat_id=target_chat_id
        )

        if not audio.delivered:
            # Запасной путь для источников, возвращающих настоящий file_id
            # (например, CustomMusicSource / HTTP API).
            await callback.message.answer_audio(
                audio=audio.telegram_file_id,
                title=audio.title,
                performer=audio.artist,
                duration=audio.duration,
                caption=_("download-caption", artist=audio.artist, title=audio.title),
            )

        keyboard = build_search_results_keyboard(result, task_id)
        # BUG FIX: wrap in _safe_edit_markup to swallow 'message is not modified'
        await _safe_edit_markup(callback.message, keyboard)

        from infrastructure.database.repositories.user_repo import UserRepository
        from infrastructure.database.session import async_session_factory
        async with async_session_factory() as session:
            await UserRepository(session).increment_requests(user.id)

    except Exception as e:
        logger.error("Ошибка скачивания трека: %s", e)
        keyboard = build_search_results_keyboard(result, task_id)
        await _safe_edit_markup(callback.message, keyboard)
        await callback.answer(_("download-error"), show_alert=True)


@router.callback_query(F.data.startswith("page:"))
async def handle_pagination(
    callback: CallbackQuery,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    parts   = callback.data.split(":", 2)
    task_id = parts[1]
    page    = int(parts[2])

    old_result = _results_cache.get(task_id)
    if not old_result:
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    await callback.answer(_("search-processing"))

    ctx = SearchContext(query=old_result.query, user_id=user.id, page=page)

    try:
        task = await queue.enqueue(ctx, is_premium=user.premium)
        result = await queue.wait_for_result(task)
        _results_cache[task_id] = result

        keyboard = build_search_results_keyboard(result, task_id)
        await _safe_edit_markup(callback.message, keyboard)

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
