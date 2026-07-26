"""
bot/handlers/admin_sources.py
Upravlenie istochnikami muzyki — FSM dobavleniya, detalі, udalenie, toggle ON/OFF.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete as sa_delete

from bot.filters.admin import IsAdmin
from infrastructure.database.repositories.source_repo import SourceRepository
from infrastructure.database.session import async_session_factory
from sources.registry import SourceRegistry

router = Router(name="admin_sources")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------


class AddSourceStates(StatesGroup):
    waiting_name     = State()
    waiting_username = State()
    waiting_priority = State()
    waiting_timeout  = State()
    preview          = State()


# ---------------------------------------------------------------------------
# Visual header / separator constants
# ---------------------------------------------------------------------------

_SEP         = "=" * 30
_HDR         = "[ADMIN] Istochniki muzyki"
_HDR_ADD     = "[ADMIN] Dobavlenie istochnika"
_HDR_DETAIL  = "[ADMIN] Istochnik"
_HDR_DELETE  = "[ADMIN] Udalenie istochnika"
_HINT_CANCEL = "\n\n/cancel — otmenit i vyyti"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_id(query_or_message: CallbackQuery | Message) -> int:
    """Возвращает telegram_id администратора из события."""
    if isinstance(query_or_message, CallbackQuery):
        return query_or_message.from_user.id if query_or_message.from_user else 0
    return query_or_message.from_user.id if query_or_message.from_user else 0


async def _safe_edit(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    from aiogram.exceptions import TelegramBadRequest
    try:
        await query.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
    await query.answer()


def _sources_markup(sources: list) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for src in sources:
        status = "ON" if src.enabled else "OFF"
        label  = "[{status}] {name} | ok:{ok} err:{err} avg:{avg}ms".format(
            status=status, name=src.name,
            ok=src.success_count, err=src.error_count,
            avg=int(src.avg_response_ms),
        )
        rows.append([InlineKeyboardButton(
            text=label,
            callback_data="admin:src:detail:{}".format(src.id),
        )])
    rows.append([InlineKeyboardButton(text="[+] Dobavit istochnik", callback_data="admin:src:add")])
    rows.append([InlineKeyboardButton(text="<< Nazad v admin",      callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_markup(src_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "[OFF] Otklyuchit" if enabled else "[ON] Vklyuchit"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text,               callback_data="admin:src:toggle:{}".format(src_id))],
        [InlineKeyboardButton(text="[X] Udalit istochnik",    callback_data="admin:src:delete:{}".format(src_id))],
        [InlineKeyboardButton(text="<< K spisku istochnikov", callback_data="admin:sources")],
    ])


def _confirm_delete_markup(src_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="[OK] Da, udalit", callback_data="admin:src:delete:confirm:{}".format(src_id)),
        InlineKeyboardButton(text="[X] Otmenit",     callback_data="admin:src:detail:{}".format(src_id)),
    ]])


def _detail_text(src) -> str:
    status  = "ON" if src.enabled else "OFF"
    created = (
        src.created_at.strftime("%d.%m.%Y")
        if isinstance(src.created_at, datetime)
        else str(src.created_at)
    )
    return (
        "{hdr} #{id}\n{sep}\n"
        "Nazvanie:  {name}\n"
        "Username:  @{username}\n"
        "Tip:       {tip}\n"
        "Prioritet: {priority}\n"
        "Timeout:   {timeout}s\n"
        "Status:    {status}\n"
        "OK: {ok} | ERR: {err} | avg: {avg}ms\n"
        "Dobavlen:  {created}"
    ).format(
        hdr=_HDR_DETAIL, sep=_SEP,
        id=src.id, name=src.name, username=src.bot_username,
        tip=src.type, priority=src.priority, timeout=src.timeout,
        status=status,
        ok=src.success_count, err=src.error_count,
        avg=int(src.avg_response_ms), created=created,
    )


def _preview_text(data: dict) -> str:
    return (
        "{hdr} — Predprosmotr\n{sep}\n"
        "Nazvanie:  {name}\n"
        "Username:  @{username}\n"
        "Prioritet: {priority}\n"
        "Timeout:   {timeout}s\n\n"
        "Proverite dannye i nazmite [OK] Dobavit."
    ).format(
        hdr=_HDR_ADD, sep=_SEP,
        name=data["name"], username=data["username"],
        priority=data["priority"], timeout=data["timeout"],
    )


def _list_text(count: int) -> str:
    return (
        "{hdr}\n{sep}\n"
        "Vsego istochnikov: {count}\n\n"
        "Nazmite na istochnik dlya upravleniya:"
    ).format(hdr=_HDR, sep=_SEP, count=count)


def _fsm_nav_markup(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="<< Nazad",    callback_data=back_data),
        InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel"),
    ]])


def _fsm_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="[OK] Dobavit", callback_data="admin:src:add:confirm"),
        InlineKeyboardButton(text="[X] Otmenit",  callback_data="admin:src:add:cancel"),
    ]])


def _fsm_cancel_only_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel"),
    ]])


# ---------------------------------------------------------------------------
# Список источников
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:sources", IsAdmin())
async def cb_sources_list(query: CallbackQuery, state: FSMContext) -> None:
    admin_id = _admin_id(query)
    logger.info("[ADMIN:%d] >>> Otkryl razdel 'Istochniki muzyki'", admin_id)
    print(f"[ADMIN:{admin_id}] >>> Otkryl razdel 'Istochniki muzyki'")

    await state.clear()
    async with async_session_factory() as session:
        repo    = SourceRepository(session)
        await repo.get_or_create_vk()
        sources = await repo.get_all()

    logger.info("[ADMIN:%d] Istochnikov v BD: %d", admin_id, len(sources))
    print(f"[ADMIN:{admin_id}] Istochnikov v BD: {len(sources)}")
    await _safe_edit(query, _list_text(len(sources)), _sources_markup(sources))


# ---------------------------------------------------------------------------
# Детальная карточка
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data and c.data.startswith("admin:src:detail:"), IsAdmin())
async def cb_source_detail(query: CallbackQuery, state: FSMContext) -> None:
    src_id   = int(query.data.split(":")[-1])
    admin_id = _admin_id(query)

    async with async_session_factory() as session:
        src = await SourceRepository(session).get_by_id(src_id)

    if src is None:
        logger.warning("[ADMIN:%d] Istochnik #%d ne naiden", admin_id, src_id)
        print(f"[ADMIN:{admin_id}] WARN: Istochnik #{src_id} ne naiden")
        await query.answer("Istochnik ne naiden.", show_alert=True)
        return

    logger.info(
        "[ADMIN:%d] >>> Prosmotr istochnika: id=%d name='%s' enabled=%s",
        admin_id, src.id, src.name, src.enabled,
    )
    print(f"[ADMIN:{admin_id}] >>> Prosmotr: id={src.id} name='{src.name}' enabled={src.enabled}")
    await _safe_edit(query, _detail_text(src), _detail_markup(src.id, src.enabled))


# ---------------------------------------------------------------------------
# Toggle enabled (ON / OFF)
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data and c.data.startswith("admin:src:toggle:"), IsAdmin())
async def cb_source_toggle(query: CallbackQuery, registry: SourceRegistry) -> None:
    src_id   = int(query.data.split(":")[-1])
    admin_id = _admin_id(query)

    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)
        if src is None:
            logger.warning("[ADMIN:%d] Toggle: istochnik #%d ne naiden", admin_id, src_id)
            print(f"[ADMIN:{admin_id}] WARN: Toggle — istochnik #{src_id} ne naiden")
            await query.answer("Istochnik ne naiden.", show_alert=True)
            return

        old_state   = src.enabled
        new_enabled = not old_state
        await repo.set_enabled(src_id, new_enabled)
        src = await repo.get_by_id(src_id)

    registry.sync_enabled(src.name, src.enabled)

    action = "VKLYUCHIL" if src.enabled else "OTKLYUCHIL"
    logger.info(
        "[ADMIN:%d] >>> %s istochnik: id=%d name='%s' (bylo=%s → stalo=%s)",
        admin_id, action, src.id, src.name, old_state, src.enabled,
    )
    print(
        f"[ADMIN:{admin_id}] >>> {action} istochnik: "
        f"id={src.id} name='{src.name}' ({old_state} -> {src.enabled})"
    )

    label = "[ON] Vklyuchen" if src.enabled else "[OFF] Otklyuchen"
    await query.answer(label)
    await _safe_edit(query, _detail_text(src), _detail_markup(src.id, src.enabled))


# ---------------------------------------------------------------------------
# Удаление — запрос подтверждения
# ---------------------------------------------------------------------------


@router.callback_query(
    lambda c: (
        c.data is not None
        and c.data.startswith("admin:src:delete:")
        and not c.data.startswith("admin:src:delete:confirm:")
    ),
    IsAdmin(),
)
async def cb_source_delete_ask(query: CallbackQuery) -> None:
    src_id   = int(query.data.split(":")[-1])
    admin_id = _admin_id(query)

    async with async_session_factory() as session:
        src = await SourceRepository(session).get_by_id(src_id)

    if src is None:
        logger.warning("[ADMIN:%d] Delete ask: istochnik #%d ne naiden", admin_id, src_id)
        print(f"[ADMIN:{admin_id}] WARN: Delete ask — istochnik #{src_id} ne naiden")
        await query.answer("Istochnik ne naiden.", show_alert=True)
        return

    logger.info(
        "[ADMIN:%d] >>> Zaprosil podtverzhdenie udaleniya: id=%d name='%s'",
        admin_id, src.id, src.name,
    )
    print(f"[ADMIN:{admin_id}] >>> Zaprosil udalenie: id={src.id} name='{src.name}'")

    text = (
        "{hdr}\n{sep}\n"
        "Vy sobiraetes udalit:\n\n"
        "Nazvanie: {name}\n"
        "Username: @{username}\n\n"
        "Eto deystvie nelzya otmenit!"
    ).format(hdr=_HDR_DELETE, sep=_SEP, name=src.name, username=src.bot_username)
    await _safe_edit(query, text, _confirm_delete_markup(src_id))


# ---------------------------------------------------------------------------
# Удаление — подтверждение
# ---------------------------------------------------------------------------


@router.callback_query(
    lambda c: c.data is not None and c.data.startswith("admin:src:delete:confirm:"),
    IsAdmin(),
)
async def cb_source_delete_confirm(query: CallbackQuery, registry: SourceRegistry) -> None:
    src_id   = int(query.data.split(":")[-1])
    admin_id = _admin_id(query)

    from infrastructure.database.models import Source

    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)
        if src is None:
            logger.warning("[ADMIN:%d] Delete confirm: istochnik #%d uzhe udalyon", admin_id, src_id)
            print(f"[ADMIN:{admin_id}] WARN: Istochnik #{src_id} uzhe udalyon")
            await query.answer("Istochnik uzhe udalyon.", show_alert=True)
            return
        src_name     = src.name
        src_username = src.bot_username
        await session.execute(sa_delete(Source).where(Source.id == src_id))
        await session.commit()

    registry.unregister(src_name)

    logger.info(
        "[ADMIN:%d] >>> UDALIL istochnik: id=%d name='%s' username='%s'",
        admin_id, src_id, src_name, src_username,
    )
    print(
        f"[ADMIN:{admin_id}] >>> UDALIL istochnik: "
        f"id={src_id} name='{src_name}' @{src_username}"
    )

    await query.answer("Istochnik udalyon.")

    async with async_session_factory() as session:
        sources = await SourceRepository(session).get_all()
    await _safe_edit(query, _list_text(len(sources)), _sources_markup(sources))


# ---------------------------------------------------------------------------
# FSM — старт добавления
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add", IsAdmin())
async def cb_source_add_start(query: CallbackQuery, state: FSMContext) -> None:
    admin_id = _admin_id(query)
    logger.info("[ADMIN:%d] >>> Nachal dobavlenie istochnika (shag 1: nazvanie)", admin_id)
    print(f"[ADMIN:{admin_id}] >>> Nachal dobavlenie istochnika")

    await state.clear()
    await state.set_state(AddSourceStates.waiting_name)
    text = (
        "{hdr}\nShag 1 iz 4: Nazvanie\n{sep}\n\n"
        "Vvedite nazvanie istochnika.\n"
        'Primer: "Spotify Bot", "VK Music Pro"'
        "{hint}"
    ).format(hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL)
    await _safe_edit(query, text, _fsm_cancel_only_markup())


# ---------------------------------------------------------------------------
# FSM — шаг 1: имя
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_name, IsAdmin())
async def fsm_waiting_name(message: Message, state: FSMContext) -> None:
    admin_id = _admin_id(message)
    name     = message.text.strip() if message.text else ""

    if not name:
        logger.warning("[ADMIN:%d] FSM shag1: pustoe nazvanie", admin_id)
        print(f"[ADMIN:{admin_id}] FSM shag1: pustoe nazvanie — otkloneno")
        await message.answer(
            "{hdr}\nShag 1 iz 4: Nazvanie\n{sep}\n\n"
            "Nazvanie ne mozhet byt pustym. Vvedite snova:{hint}".format(
                hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
            reply_markup=_fsm_cancel_only_markup(),
        )
        return

    logger.info("[ADMIN:%d] FSM shag1: vvel nazvanie='%s'", admin_id, name)
    print(f"[ADMIN:{admin_id}] FSM shag1: nazvanie='{name}' -> perekhod na shag 2")

    await state.update_data(name=name)
    await state.set_state(AddSourceStates.waiting_username)
    await message.answer(
        "{hdr}\nShag 2 iz 4: Username\n{sep}\n\n"
        "Vvedite @username bota-istochnika (bez @).\n"
        "Primer: vkmusic_bot, spotify_dl_bot{hint}".format(
            hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
        reply_markup=_fsm_nav_markup("admin:src:add:back:name"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 2: username
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_username, IsAdmin())
async def fsm_waiting_username(message: Message, state: FSMContext) -> None:
    admin_id = _admin_id(message)
    username = (message.text or "").strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        logger.warning("[ADMIN:%d] FSM shag2: nekorektny username='%s'", admin_id, username)
        print(f"[ADMIN:{admin_id}] FSM shag2: WARN nekorektny username='{username}' — otkloneno")
        await message.answer(
            "{hdr}\nShag 2 iz 4: Username\n{sep}\n\n"
            "Nekorektny username.\n"
            "Ispolzuyte latinskie bukvy, tsifry i _. Dlina 3-64.\n"
            "Vvedite snova:{hint}".format(hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
            reply_markup=_fsm_nav_markup("admin:src:add:back:name"),
        )
        return

    logger.info("[ADMIN:%d] FSM shag2: vvel username='%s'", admin_id, username)
    print(f"[ADMIN:{admin_id}] FSM shag2: username='@{username}' -> perekhod na shag 3")

    await state.update_data(username=username)
    await state.set_state(AddSourceStates.waiting_priority)
    await message.answer(
        "{hdr}\nShag 3 iz 4: Prioritet\n{sep}\n\n"
        "Vvedite prioritet — tseloe chislo ot 1 do 100.\n"
        "Chem vyshe, tem chashche budet ispolzovatsya etot istochnik.{hint}".format(
            hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
        reply_markup=_fsm_nav_markup("admin:src:add:back:username"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 3: приоритет
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_priority, IsAdmin())
async def fsm_waiting_priority(message: Message, state: FSMContext) -> None:
    admin_id = _admin_id(message)
    raw      = (message.text or "").strip()

    if not raw.isdigit() or not (1 <= int(raw) <= 100):
        logger.warning("[ADMIN:%d] FSM shag3: nekorektny prioritet='%s'", admin_id, raw)
        print(f"[ADMIN:{admin_id}] FSM shag3: WARN nekorektny prioritet='{raw}' — otkloneno")
        await message.answer(
            "{hdr}\nShag 3 iz 4: Prioritet\n{sep}\n\n"
            "Nekorektny prioritet. Vvedite tseloe chislo ot 1 do 100:{hint}".format(
                hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
            reply_markup=_fsm_nav_markup("admin:src:add:back:username"),
        )
        return

    logger.info("[ADMIN:%d] FSM shag3: vvel prioritet=%s", admin_id, raw)
    print(f"[ADMIN:{admin_id}] FSM shag3: prioritet={raw} -> perekhod na shag 4")

    await state.update_data(priority=int(raw))
    await state.set_state(AddSourceStates.waiting_timeout)
    await message.answer(
        "{hdr}\nShag 4 iz 4: Timeout\n{sep}\n\n"
        "Vvedite maksimalnoye vremya ozhidaniya otveta v sekundakh (10-120).\n"
        "Rekomenduetsya: 30{hint}".format(hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
        reply_markup=_fsm_nav_markup("admin:src:add:back:priority"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 4: таймаут → предпросмотр
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_timeout, IsAdmin())
async def fsm_waiting_timeout(message: Message, state: FSMContext) -> None:
    admin_id = _admin_id(message)
    raw      = (message.text or "").strip()

    if not raw.isdigit() or not (10 <= int(raw) <= 120):
        logger.warning("[ADMIN:%d] FSM shag4: nekorektny timeout='%s'", admin_id, raw)
        print(f"[ADMIN:{admin_id}] FSM shag4: WARN nekorektny timeout='{raw}' — otkloneno")
        await message.answer(
            "{hdr}\nShag 4 iz 4: Timeout\n{sep}\n\n"
            "Nekorektny timeout. Vvedite tseloe chislo ot 10 do 120:{hint}".format(
                hdr=_HDR_ADD, sep=_SEP, hint=_HINT_CANCEL),
            reply_markup=_fsm_nav_markup("admin:src:add:back:priority"),
        )
        return

    data = {**(await state.get_data()), "timeout": int(raw)}
    logger.info(
        "[ADMIN:%d] FSM shag4: vvel timeout=%s — vse dannye: name='%s' username='%s' priority=%s timeout=%s",
        admin_id, raw,
        data.get("name"), data.get("username"), data.get("priority"), raw,
    )
    print(
        f"[ADMIN:{admin_id}] FSM shag4: timeout={raw} — "
        f"predprosmotr: name='{data.get('name')}' @{data.get('username')} "
        f"priority={data.get('priority')} timeout={raw}"
    )

    await state.update_data(timeout=int(raw))
    await state.set_state(AddSourceStates.preview)
    await message.answer(_preview_text(data), reply_markup=_fsm_confirm_markup())


# ---------------------------------------------------------------------------
# FSM — Back-навигация (callback)
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add:back:name", IsAdmin())
async def fsm_back_to_name(query: CallbackQuery, state: FSMContext) -> None:
    admin_id = _admin_id(query)
    logger.info("[ADMIN:%d] FSM back: vernulsya na shag 1 (nazvanie)", admin_id)
    print(f"[ADMIN:{admin_id}] FSM back -> shag 1 nazvanie")

    await state.set_state(AddSourceStates.waiting_name)
    data = await state.get_data()
    hint = ' (tekushchee: "{val}")'.format(val=data["name"]) if data.get("name") else ""
    await _safe_edit(
        query,
        "{hdr}\nShag 1 iz 4: Nazvanie\n{sep}\n\n"
        "Vvedite nazvanie istochnika{hint}:{cancel}".format(
            hdr=_HDR_ADD, sep=_SEP, hint=hint, cancel=_HINT_CANCEL),
        _fsm_cancel_only_markup(),
    )


@router.callback_query(lambda c: c.data == "admin:src:add:back:username", IsAdmin())
async def fsm_back_to_username(query: CallbackQuery, state: FSMContext) -> None:
    admin_id = _admin_id(query)
    logger.info("[ADMIN:%d] FSM back: vernulsya na shag 2 (username)", admin_id)
    print(f"[ADMIN:{admin_id}] FSM back -> shag 2 username")

    await state.set_state(AddSourceStates.waiting_username)
    data = await state.get_data()
    hint = " (tekushchee: @{val})".format(val=data["username"]) if data.get("username") else ""
    await _safe_edit(
        query,
        "{hdr}\nShag 2 iz 4: Username\n{sep}\n\n"
        "Vvedite @username bota-istochnika (bez @){hint}:{cancel}".format(
            hdr=_HDR_ADD, sep=_SEP, hint=hint, cancel=_HINT_CANCEL),
        _fsm_nav_markup("admin:src:add:back:name"),
    )


@router.callback_query(lambda c: c.data == "admin:src:add:back:priority", IsAdmin())
async def fsm_back_to_priority(query: CallbackQuery, state: FSMContext) -> None:
    admin_id = _admin_id(query)
    logger.info("[ADMIN:%d] FSM back: vernulsya na shag 3 (prioritet)", admin_id)
    print(f"[ADMIN:{admin_id}] FSM back -> shag 3 prioritet")

    await state.set_state(AddSourceStates.waiting_priority)
    data = await state.get_data()
    hint = " (tekushchee: {val})".format(val=data["priority"]) if data.get("priority") else ""
    await _safe_edit(
        query,
        "{hdr}\nShag 3 iz 4: Prioritet\n{sep}\n\n"
        "Vvedite prioritet (1-100){hint}:{cancel}".format(
            hdr=_HDR_ADD, sep=_SEP, hint=hint, cancel=_HINT_CANCEL),
        _fsm_nav_markup("admin:src:add:back:username"),
    )


# ---------------------------------------------------------------------------
# FSM — отмена (callback и /cancel)
# ---------------------------------------------------------------------------


async def _fsm_cancel_to_list(state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    await state.clear()
    async with async_session_factory() as session:
        sources = await SourceRepository(session).get_all()
    return _list_text(len(sources)), _sources_markup(sources)


@router.callback_query(lambda c: c.data == "admin:src:add:cancel", IsAdmin())
async def fsm_cancel_callback(query: CallbackQuery, state: FSMContext) -> None:
    admin_id    = _admin_id(query)
    prev_state  = await state.get_state()
    logger.info("[ADMIN:%d] >>> OTMENIL dobavlenie (byl v sostoyanii: %s)", admin_id, prev_state)
    print(f"[ADMIN:{admin_id}] >>> OTMENIL dobavlenie (state={prev_state})")

    text, markup = await _fsm_cancel_to_list(state)
    await _safe_edit(query, text, markup)


@router.message(Command("cancel"), IsAdmin())
async def fsm_cancel_command(message: Message, state: FSMContext) -> None:
    admin_id   = _admin_id(message)
    prev_state = await state.get_state()

    if prev_state is None:
        logger.info("[ADMIN:%d] /cancel — net aktivnogo FSM", admin_id)
        print(f"[ADMIN:{admin_id}] /cancel — net aktivnogo FSM")
        await message.answer("[ADMIN] Nichego ne otmenyaem — net aktivnogo deystviya.")
        return

    logger.info("[ADMIN:%d] >>> /cancel — otmenil iz sostoyaniya: %s", admin_id, prev_state)
    print(f"[ADMIN:{admin_id}] >>> /cancel — otmenil (state={prev_state})")

    text, markup = await _fsm_cancel_to_list(state)
    await message.answer("[ADMIN] Deystvie otmeneno.\n\n" + text, reply_markup=markup)


# ---------------------------------------------------------------------------
# FSM — подтверждение создания
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add:confirm", IsAdmin())
async def fsm_confirm_create(
    query: CallbackQuery,
    state: FSMContext,
    registry: SourceRegistry,
) -> None:
    admin_id = _admin_id(query)
    data     = await state.get_data()
    await state.clear()

    logger.info(
        "[ADMIN:%d] >>> SOZDAET istochnik: name='%s' username='%s' priority=%s timeout=%s",
        admin_id, data.get("name"), data.get("username"), data.get("priority"), data.get("timeout"),
    )
    print(
        f"[ADMIN:{admin_id}] >>> SOZDAET istochnik: "
        f"name='{data.get('name')}' @{data.get('username')} "
        f"priority={data.get('priority')} timeout={data.get('timeout')}"
    )

    from infrastructure.database.models import Source
    from sources.vk_music_bot import VKMusicBotSource

    new_source = Source(
        name=data["name"],
        bot_username=data["username"],
        type="telegram_bot",
        priority=data["priority"],
        timeout=data["timeout"],
        enabled=True,
        success_count=0,
        error_count=0,
        avg_response_ms=0.0,
        created_at=datetime.utcnow(),
    )

    async with async_session_factory() as session:
        session.add(new_source)
        await session.commit()
        await session.refresh(new_source)
        src_id = new_source.id

    # Регистрируем в in-memory реестре
    new_mem_source = VKMusicBotSource(
        client=None,  # type: ignore[arg-type]
        priority=data["priority"],
        enabled=True,
    )
    new_mem_source.name         = data["name"]
    new_mem_source.bot_username = data["username"]
    registry.register(new_mem_source)

    logger.info(
        "[ADMIN:%d] >>> SOZDAN istochnik: id=%d name='%s' — zaregistrirovan v registry",
        admin_id, src_id, data["name"],
    )
    print(
        f"[ADMIN:{admin_id}] >>> SOZDAN istochnik: "
        f"id={src_id} name='{data['name']}' — zaregistrirovan v registry"
    )

    await query.answer("[ADMIN] Istochnik dobavlen!")

    async with async_session_factory() as session:
        src = await SourceRepository(session).get_by_id(src_id)

    if src:
        await _safe_edit(query, _detail_text(src), _detail_markup(src.id, src.enabled))
    else:
        text, markup = await _fsm_cancel_to_list(state)
        await _safe_edit(query, text, markup)
