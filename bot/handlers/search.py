import asyncio
import logging
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import default_state
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


@router.message(F.text & ~F.text.startswith("/"), StateFilter(default_state))
async def handle_search_query(
    message: Message,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    query = message.text.strip()
    print(f"[search] handle_search_query: user_id={user.id} query={query!r}")

    if not query or len(query) < 2:
        print(f"[search] query too short, answering search-too-short")
        await message.answer(_("search-too-short"))
        return

    wait_msg = await message.answer(_("search-processing"))
    print(f"[search] sent wait_msg: {_('search-processing')!r}")

    ctx = SearchContext(query=query, user_id=user.id, page=1)

    try:
        task = await queue.enqueue(ctx, is_premium=user.premium)
        print(f"[search] task enqueued: task_id={task.task_id} is_premium={user.premium}")
        pos = queue.get_position(task.task_id)
        print(f"[search] queue position={pos}")

        if pos and pos > 1:
            text = _("search-queue-position", position=pos, query=query)
            print(f"[search] editing wait_msg to queue position: {text!r}")
            await wait_msg.edit_text(text)

        result = await queue.wait_for_result(task)
        print(f"[search] got result: tracks={len(result.tracks)} total={result.total}")

    except PermissionError as e:
        print(f"[search] PermissionError: {e}")
        await wait_msg.edit_text(f"⏳ {e}")
        return
    except asyncio.TimeoutError:
        print(f"[search] TimeoutError — answering search-timeout")
        await wait_msg.edit_text(_("search-timeout"))
        return
    except OverflowError:
        print(f"[search] OverflowError — answering search-queue-full")
        await wait_msg.edit_text(_("search-queue-full"))
        return
    except Exception as e:
        print(f"[search] Exception: {e}")
        logger.error("Ошибка поиска для %d: %s", user.telegram_id, e)
        await wait_msg.edit_text(_("search-error"))
        return

    if not result.tracks:
        print(f"[search] no tracks found for query={query!r}")
        await wait_msg.edit_text(_("search-not-found", query=query))
        return

    _results_cache[task.task_id] = result

    keyboard = build_search_results_keyboard(result, task.task_id)
    header = _("search-results-header", query=query, total=result.total)
    print(f"[search] sending results: {header!r}")
    await wait_msg.edit_text(header, reply_markup=keyboard)


@router.callback_query(F.data.startswith("dl:"))
async def handle_download(
    callback: CallbackQuery,
    user: User,
    search_manager: SearchManager,
    cache: CacheManager,
    _: Callable,
) -> None:
    print(f"[search] handle_download: callback.data={callback.data!r} user_id={user.id}")
    # BUG FIX: используем _cb вместо _ чтобы не перезаписать переводчик _
    _cb, task_id, track_idx_str = callback.data.split(":", 2)
    track_idx = int(track_idx_str)
    print(f"[search] task_id={task_id} track_idx={track_idx}")

    result = _results_cache.get(task_id)
    if not result or track_idx >= len(result.tracks):
        print(f"[search] result not found or stale: result={result}")
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    track = result.tracks[track_idx]
    print(f"[search] track: artist={track.artist!r} title={track.title!r}")

    await callback.message.edit_reply_markup(reply_markup=build_downloading_keyboard())
    await callback.answer()

    try:
        print(f"[search] calling get_audio: track={track.title!r} target_chat_id={callback.from_user.id}")
        audio = await search_manager.get_audio(track, user.id)
        print(f"[search] got audio: file_id={audio.telegram_file_id!r} already_sent={audio.already_sent}")

        print(f"[search] sending audio to chat")
        await callback.message.answer_audio(
            audio=audio.telegram_file_id,
            title=audio.title,
            performer=audio.artist,
            duration=audio.duration,
            caption=_("download-caption", artist=audio.artist, title=audio.title),
        )
        print(f"[search] audio sent successfully")

        keyboard = build_search_results_keyboard(result, task_id)
        await callback.message.edit_reply_markup(reply_markup=keyboard)

        from infrastructure.database.repositories.user_repo import UserRepository
        from infrastructure.database.session import async_session_factory
        async with async_session_factory() as session:
            await UserRepository(session).increment_requests(user.id)
        print(f"[search] incremented requests for user_id={user.id}")

    except Exception as e:
        print(f"[search] Exception in handle_download: {e}")
        logger.error("Ошибка скачивания трека: %s", e)
        keyboard = build_search_results_keyboard(result, task_id)
        await callback.message.edit_reply_markup(reply_markup=keyboard)
        await callback.answer(_("download-error"), show_alert=True)


@router.callback_query(F.data.startswith("page:"))
async def handle_pagination(
    callback: CallbackQuery,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    print(f"[search] handle_pagination: callback.data={callback.data!r} user_id={user.id}")
    # BUG FIX: используем _cb вместо _ чтобы не перезаписать переводчик _
    _cb, task_id, page_str = callback.data.split(":", 2)
    page = int(page_str)
    print(f"[search] task_id={task_id} page={page}")

    old_result = _results_cache.get(task_id)
    if not old_result:
        print(f"[search] old_result not found, stale")
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    await callback.answer(_("search-processing"))

    ctx = SearchContext(query=old_result.query, user_id=user.id, page=page)
    print(f"[search] paginating query={old_result.query!r} page={page}")

    try:
        task = await queue.enqueue(ctx, is_premium=user.premium)
        result = await queue.wait_for_result(task)
        print(f"[search] pagination result: tracks={len(result.tracks)} total={result.total}")
        _results_cache[task_id] = result

        keyboard = build_search_results_keyboard(result, task_id)
        await callback.message.edit_reply_markup(reply_markup=keyboard)

    except Exception as e:
        print(f"[search] Exception in handle_pagination: {e}")
        logger.error("Ошибка пагинации: %s", e)
        await callback.answer(_("search-error"), show_alert=True)


@router.callback_query(F.data == "close")
async def handle_close(callback: CallbackQuery) -> None:
    print(f"[search] handle_close: user_id={callback.from_user.id if callback.from_user else '?'}")
    await callback.message.delete()
    await callback.answer()


@router.callback_query(F.data == "noop")
async def handle_noop(callback: CallbackQuery) -> None:
    print(f"[search] handle_noop: user_id={callback.from_user.id if callback.from_user else '?'}")
    await callback.answer()
