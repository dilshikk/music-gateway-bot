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
from sources.base import SearchResult

logger = logging.getLogger(__name__)
router = Router(name="search")

# { user_id: { "search_results": SearchResult|None, "current_query": str } }
# Каждый пользователь имеет изолированную сессию — результаты не пересекаются.
_user_sessions: dict[int, dict] = {}


def _get_user_session(user_id: int) -> dict:
    if user_id not in _user_sessions:
        _user_sessions[user_id] = {
            "search_results": None,
            "active_source": None,
            "current_query": "",
            "waiting_for_track": False,
        }
    return _user_sessions[user_id]


@router.message(F.text & ~F.text.startswith("/"), StateFilter(default_state))
async def handle_search_query(
    message: Message,
    user: User,
    queue: QueueManager,
    _: Callable,
) -> None:
    query = message.text.strip()
    print(f"[search] handle_search_query: user_id={user.id} telegram_id={message.from_user.id} query={query!r}")

    if not query or len(query) < 2:
        print("[search] query too short")
        await message.answer(_("search-too-short"))
        return

    wait_msg = await message.answer(_("search-processing"))
    print(f"[search] sent wait_msg")

    ctx = SearchContext(query=query, user_id=user.id, page=1)

    try:
        task = await queue.enqueue(ctx, is_premium=user.premium)
        print(f"[search] task enqueued: task_id={task.task_id} is_premium={user.premium}")
        pos = queue.get_position(task.task_id)
        print(f"[search] queue position={pos}")

        if pos and pos > 1:
            text = _("search-queue-position", position=pos, query=query)
            print(f"[search] queue position text: {text!r}")
            await wait_msg.edit_text(text)

        result = await queue.wait_for_result(task)
        print(f"[search] got result: tracks={len(result.tracks)} total={result.total}")

    except PermissionError as e:
        print(f"[search] PermissionError: {e}")
        await wait_msg.edit_text(f"⏳ {e}")
        return
    except asyncio.TimeoutError:
        print("[search] TimeoutError")
        await wait_msg.edit_text(_("search-timeout"))
        return
    except OverflowError:
        print("[search] OverflowError — queue full")
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

    session = _get_user_session(user.id)
    session["search_results"] = result
    session["current_query"] = query
    print(f"[search] saved to session: user_id={user.id} tracks={len(result.tracks)}")
    print(f"[search] active sessions: {list(_user_sessions.keys())}")

    keyboard = build_search_results_keyboard(result, task.task_id)
    header = _("search-results-header", query=query, total=result.total)
    print(f"[search] sending results header: {header!r}")
    await wait_msg.edit_text(header, reply_markup=keyboard)


@router.callback_query(F.data.startswith("dl:"))
async def handle_download(
    callback: CallbackQuery,
    user: User,
    search_manager: SearchManager,
    cache: CacheManager,
    _: Callable,
) -> None:
    # Telegram chat_id пользователя — именно его нужно передать в get_audio
    # чтобы userbot переслал аудио в группу с caption="user:{telegram_chat_id}"
    telegram_chat_id = callback.from_user.id

    print(f"[search] handle_download: callback.data={callback.data!r} user_id={user.id} telegram_chat_id={telegram_chat_id}")
    _cb, task_id, track_idx_str = callback.data.split(":", 2)
    track_idx = int(track_idx_str)
    print(f"[search] task_id={task_id} track_idx={track_idx}")

    session = _get_user_session(user.id)
    result: SearchResult | None = session.get("search_results")
    print(f"[search] session for user_id={user.id}: result={'found' if result else 'None'}")

    if not result or track_idx >= len(result.tracks):
        print(f"[search] result not found or stale")
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    track = result.tracks[track_idx]
    print(f"[search] track: artist={track.artist!r} title={track.title!r}")

    await callback.message.edit_reply_markup(reply_markup=build_downloading_keyboard())
    await callback.answer()

    try:
        # ВАЖНО: передаём telegram_chat_id (не user.id из БД!)
        # Именно этот ID попадёт в caption="user:{telegram_chat_id}" в группе,
        # и relay.py отправит аудио в правильный чат.
        print(f"[search] calling get_audio: track={track.title!r} target_chat_id={telegram_chat_id}")
        audio = await search_manager.get_audio(
            track,
            user.id,
            target_chat_id=telegram_chat_id,
        )
        print(f"[search] got audio: file_id={audio.telegram_file_id!r} already_sent={audio.already_sent}")

        if audio.already_sent:
            # relay.py уже отправил аудио пользователю через группу — ничего не делаем
            print(f"[search] already_sent=True — relay.py перешлёт пользователю, пропускаем send_audio")
        else:
            # Fallback: LOG_GROUP_ID не настроен — отправляем напрямую
            # ВНИМАНИЕ: file_id от Pyrogram не работает в Bot API.
            # Этот путь работает только если file_id получен через Bot API (кэш и т.п.)
            print(f"[search] already_sent=False — отправляем напрямую (fallback, LOG_GROUP_ID не настроен)")
            await callback.message.answer_audio(
                audio=audio.telegram_file_id,
                title=audio.title,
                performer=audio.artist,
                duration=audio.duration,
                caption=_("download-caption", artist=audio.artist, title=audio.title),
            )
            print("[search] audio sent directly")

        keyboard = build_search_results_keyboard(result, task_id)
        await callback.message.edit_reply_markup(reply_markup=keyboard)

        from infrastructure.database.repositories.user_repo import UserRepository
        from infrastructure.database.session import async_session_factory
        async with async_session_factory() as session_db:
            await UserRepository(session_db).increment_requests(user.id)
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
    _cb, task_id, page_str = callback.data.split(":", 2)
    page = int(page_str)
    print(f"[search] task_id={task_id} page={page}")

    session = _get_user_session(user.id)
    old_result: SearchResult | None = session.get("search_results")
    print(f"[search] session for user_id={user.id}: result={'found' if old_result else 'None'}")

    if not old_result:
        print("[search] old_result not found, stale")
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    await callback.answer(_("search-processing"))

    ctx = SearchContext(query=old_result.query, user_id=user.id, page=page)
    print(f"[search] paginating query={old_result.query!r} page={page}")

    try:
        task = await queue.enqueue(ctx, is_premium=user.premium)
        result = await queue.wait_for_result(task)
        print(f"[search] pagination result: tracks={len(result.tracks)} total={result.total}")

        session["search_results"] = result
        print(f"[search] updated session for user_id={user.id}")

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
