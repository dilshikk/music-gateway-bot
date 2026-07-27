import asyncio
import logging
import time
from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.state import default_state
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest

from bot.keyboards.search import (
    build_downloading_keyboard,
    build_search_results_keyboard,
    build_track_list_text,
)
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.search_manager import SearchContext, SearchManager
from infrastructure.database.models import User
from sources.base import SearchResult, TrackNotFoundError

logger = logging.getLogger(__name__)
router = Router(name="search")

# ─── Сессии пользователей ─────────────────────────────────────────────────────
# { user_id: { "search_results": SearchResult|None, ..., "updated_at": float } }
# Каждый пользователь имеет изолированную сессию — результаты не пересекаются.
_user_sessions: dict[int, dict] = {}

SESSION_TTL      = 15 * 60  # 15 мин — синхронизировано с STALE_TTL в VKMusicBotSource
CLEANUP_INTERVAL = 5  * 60  # как часто фоновая задача чистит устаревшие сессии

_cleanup_task: asyncio.Task | None = None


def _get_user_session(user_id: int) -> dict:
    """
    Возвращает сессию пользователя, создавая её при необходимости.
    Если сессия существует, но устарела (> SESSION_TTL), сбрасывает её —
    ленивая защита от использования протухших результатов поиска.
    """
    session = _user_sessions.get(user_id)
    now = time.monotonic()

    if session is not None and (now - session.get("updated_at", 0)) > SESSION_TTL:
        print(f"[search] сессия user_id={user_id} протухла (TTL={SESSION_TTL}s) — сбрасываем")
        session = None

    if session is None:
        session = {
            "search_results": None,
            "active_source": None,
            "current_query": "",
            "waiting_for_track": False,
            "updated_at": now,
        }
        _user_sessions[user_id] = session

    return session


def _touch_session(session: dict) -> None:
    """Обновляет метку времени активности сессии."""
    session["updated_at"] = time.monotonic()


async def _cleanup_loop() -> None:
    """
    Фоновая задача: каждые CLEANUP_INTERVAL секунд физически удаляет
    из памяти сессии, по которым давно не было активности.
    Без этого dict растёт вечно — каждый новый user_id добавляет запись,
    которая никогда не удаляется.
    """
    print("[search] cleanup_loop запущен")
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL)
        now = time.monotonic()
        stale = [
            uid for uid, s in list(_user_sessions.items())
            if (now - s.get("updated_at", 0)) > SESSION_TTL
        ]
        for uid in stale:
            _user_sessions.pop(uid, None)
        if stale:
            print(
                f"[search] cleanup: удалено {len(stale)} протухших сессий  "
                f"осталось={len(_user_sessions)}"
            )
            logger.info(
                "[search] cleanup: удалено %d сессий, осталось %d",
                len(stale), len(_user_sessions),
            )


def start_cleanup_task() -> None:
    """Запускает фоновую очистку сессий. Вызывать один раз в on_startup."""
    global _cleanup_task
    if _cleanup_task and not _cleanup_task.done():
        print("[search] cleanup_task уже запущен")
        return
    _cleanup_task = asyncio.create_task(_cleanup_loop(), name="search-session-cleanup")
    print("[search] cleanup_task создан")


async def stop_cleanup_task() -> None:
    """Останавливает фоновую очистку. Вызывать в on_shutdown."""
    global _cleanup_task
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        print("[search] cleanup_task остановлен")


# ─── Вспомогательные функции ─────────────────────────────────────────────────

def _build_results_message(result: SearchResult, query: str, _: Callable) -> str:
    """Текст сообщения: заголовок + нумерованный список треков."""
    header = (
        f"🔍 {query}\n"
        f"Результаты {(result.page - 1) * 8 + 1}–"
        f"{(result.page - 1) * 8 + len(result.tracks)} из {result.total}\n\n"
    )
    return header + build_track_list_text(result)


# ─── Хендлеры ────────────────────────────────────────────────────────────────

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
    print("[search] sent wait_msg")

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
    _touch_session(session)
    print(f"[search] saved to session: user_id={user.id} tracks={len(result.tracks)}")
    print(f"[search] active sessions: {list(_user_sessions.keys())}")

    keyboard = build_search_results_keyboard(result, task.task_id)
    text = _build_results_message(result, query, _)
    print("[search] sending results")
    await wait_msg.edit_text(text, reply_markup=keyboard)


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
    # Любая активность продлевает жизнь сессии
    _touch_session(session)
    print(f"[search] session for user_id={user.id}: result={'found' if result else 'None'}")

    if not result or track_idx >= len(result.tracks):
        print("[search] result not found or stale")
        await callback.answer(_("download-results-stale"), show_alert=True)
        return

    track = result.tracks[track_idx]
    print(f"[search] track: artist={track.artist!r} title={track.title!r}")

    await callback.message.edit_reply_markup(reply_markup=build_downloading_keyboard())
    await callback.answer()

    try:
        print(f"[search] calling get_audio: track={track.title!r} target_chat_id={telegram_chat_id}")
        audio = await search_manager.get_audio(
            track,
            user.id,
            target_chat_id=telegram_chat_id,
        )
        print(f"[search] got audio: file_id={audio.telegram_file_id!r} already_sent={audio.already_sent}")

        if audio.already_sent:
            print("[search] already_sent=True — relay.py перешлёт пользователю, пропускаем send_audio")
        else:
            print("[search] already_sent=False — отправляем напрямую (fallback)")
            await callback.message.answer_audio(
                audio=audio.telegram_file_id,
                title=audio.title,
                performer=audio.artist,
                duration=audio.duration,
                caption=_("download-caption", artist=audio.artist, title=audio.title),
            )
            print("[search] audio sent directly")

        keyboard = build_search_results_keyboard(result, task_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                print("[search] keyboard not modified — ignoring")
            else:
                raise

        from infrastructure.database.repositories.user_repo import UserRepository
        from infrastructure.database.session import async_session_factory
        async with async_session_factory() as session_db:
            await UserRepository(session_db).increment_requests(user.id)
        print(f"[search] incremented requests for user_id={user.id}")

    except TrackNotFoundError as e:
        # Результаты устарели или трек недоступен — показываем понятную ошибку
        print(f"[search] TrackNotFoundError: {e}")
        keyboard = build_search_results_keyboard(result, task_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
        await callback.answer(_("download-results-stale"), show_alert=True)

    except Exception as e:
        print(f"[search] Exception in handle_download: {e}")
        logger.error("Ошибка скачивания трека: %s", e)
        keyboard = build_search_results_keyboard(result, task_id)
        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except TelegramBadRequest:
            pass
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
        _touch_session(session)
        print(f"[search] updated session for user_id={user.id}")

        keyboard = build_search_results_keyboard(result, task_id)
        text = _build_results_message(result, old_result.query, _)
        try:
            await callback.message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest as e:
            if "message is not modified" in str(e):
                print("[search] pagination content not modified — ignoring")
            else:
                raise

    except TelegramBadRequest:
        pass
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
