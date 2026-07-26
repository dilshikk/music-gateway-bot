"""
bot/handlers/admin_sources.py
Upravlenie istochnikami muzyki — FSM dobavleniya, detalі, udalenie, toggle ON/OFF.
"""

from __future__ import annotations

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

router = Router(name="admin_sources")

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
# Helpers
# ---------------------------------------------------------------------------


async def _safe_edit(
    query: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Redaktiruet soobshenie, ignoriruya 'not modified'."""
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
        status  = "ON" if src.enabled else "OFF"
        ok_cnt  = src.success_count
        err_cnt = src.error_count
        avg_ms  = int(src.avg_response_ms)
        label   = "[{status}] {name} | ok:{ok} err:{err} avg:{avg}ms".format(
            status=status,
            name=src.name,
            ok=ok_cnt,
            err=err_cnt,
            avg=avg_ms,
        )
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data="admin:src:detail:{}".format(src.id),
            )
        ])
    rows.append([
        InlineKeyboardButton(text="[+] Dobavit", callback_data="admin:src:add"),
    ])
    rows.append([
        InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _detail_markup(src_id: int, enabled: bool) -> InlineKeyboardMarkup:
    toggle_text = "[OFF] Otklyuchit" if enabled else "[ON] Vklyuchit"
    toggle_data = "admin:src:toggle:{}".format(src_id)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_text, callback_data=toggle_data)],
        [InlineKeyboardButton(text="[X] Udalit",  callback_data="admin:src:delete:{}".format(src_id))],
        [InlineKeyboardButton(text="<< Nazad",    callback_data="admin:sources")],
    ])


def _confirm_delete_markup(src_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="[OK] Da, udalit",
                callback_data="admin:src:delete:confirm:{}".format(src_id),
            ),
            InlineKeyboardButton(
                text="[X] Otmenit",
                callback_data="admin:src:detail:{}".format(src_id),
            ),
        ]
    ])


def _detail_text(src) -> str:
    status  = "ON" if src.enabled else "OFF"
    created = (
        src.created_at.strftime("%d.%m.%Y")
        if isinstance(src.created_at, datetime)
        else str(src.created_at)
    )
    avg_ms = int(src.avg_response_ms)
    return (
        "=== Istochnik #{id} ===\n\n"
        "Nazvanie:  {name}\n"
        "Username:  @{username}\n"
        "Tip:       {tip}\n"
        "Prioritet: {priority}\n"
        "Timeout:   {timeout}s\n"
        "Status:    {status}\n"
        "OK: {ok} | ERR: {err} | avg: {avg}ms\n"
        "Dobavlen:  {created}"
    ).format(
        id=src.id,
        name=src.name,
        username=src.bot_username,
        tip=src.type,
        priority=src.priority,
        timeout=src.timeout,
        status=status,
        ok=src.success_count,
        err=src.error_count,
        avg=avg_ms,
        created=created,
    )


def _preview_text(data: dict) -> str:
    return (
        "=== Novyi istochnik ===\n\n"
        "Nazvanie:  {name}\n"
        "Username:  @{username}\n"
        "Prioritet: {priority}\n"
        "Timeout:   {timeout}s\n\n"
        "Dobavit?"
    ).format(
        name=data["name"],
        username=data["username"],
        priority=data["priority"],
        timeout=data["timeout"],
    )


def _fsm_nav_markup(back_data: str) -> InlineKeyboardMarkup:
    """Klaviatura Back / Cancel dlya shagov FSM."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="<< Nazad",    callback_data=back_data),
            InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel"),
        ]
    ])


def _fsm_confirm_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="[OK] Dobavit", callback_data="admin:src:add:confirm"),
            InlineKeyboardButton(text="[X] Otmenit",  callback_data="admin:src:add:cancel"),
        ]
    ])


# ---------------------------------------------------------------------------
# Список источников
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:sources", IsAdmin())
async def cb_sources_list(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    async with async_session_factory() as session:
        repo    = SourceRepository(session)
        await repo.get_or_create_vk()
        sources = await repo.get_all()

    count  = len(sources)
    text   = "=== Istochniki muzyki ({}) ===\n\nVyberite istochnik dlya upravleniya:".format(count)
    markup = _sources_markup(sources)
    await _safe_edit(query, text, markup)


# ---------------------------------------------------------------------------
# Детальная карточка
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data and c.data.startswith("admin:src:detail:"), IsAdmin())
async def cb_source_detail(query: CallbackQuery, state: FSMContext) -> None:
    src_id = int(query.data.split(":")[-1])
    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)

    if src is None:
        await query.answer("Istochnik ne naiden.", show_alert=True)
        return

    await _safe_edit(query, _detail_text(src), _detail_markup(src.id, src.enabled))


# ---------------------------------------------------------------------------
# Toggle enabled (ON / OFF)
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data and c.data.startswith("admin:src:toggle:"), IsAdmin())
async def cb_source_toggle(query: CallbackQuery) -> None:
    src_id = int(query.data.split(":")[-1])
    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)
        if src is None:
            await query.answer("Istochnik ne naiden.", show_alert=True)
            return
        new_state = not src.enabled
        await repo.set_enabled(src_id, new_state)
        src = await repo.get_by_id(src_id)

    label = "Vklyuchen" if src.enabled else "Otklyuchen"
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
    src_id = int(query.data.split(":")[-1])
    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)

    if src is None:
        await query.answer("Istochnik ne naiden.", show_alert=True)
        return

    text = 'Udalit istochnik "{name}"? Eto deystvie nelzya otmenit.'.format(name=src.name)
    await _safe_edit(query, text, _confirm_delete_markup(src_id))


# ---------------------------------------------------------------------------
# Удаление — подтверждение
# ---------------------------------------------------------------------------


@router.callback_query(
    lambda c: c.data is not None and c.data.startswith("admin:src:delete:confirm:"),
    IsAdmin(),
)
async def cb_source_delete_confirm(query: CallbackQuery) -> None:
    src_id = int(query.data.split(":")[-1])

    from infrastructure.database.models import Source

    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)
        if src is None:
            await query.answer("Istochnik uzhe udalyon.", show_alert=True)
            return
        await session.execute(sa_delete(Source).where(Source.id == src_id))
        await session.commit()

    await query.answer("Istochnik udalyon.")

    async with async_session_factory() as session:
        repo    = SourceRepository(session)
        sources = await repo.get_all()

    count  = len(sources)
    text   = "=== Istochniki muzyki ({}) ===\n\nVyberite istochnik dlya upravleniya:".format(count)
    markup = _sources_markup(sources)
    await _safe_edit(query, text, markup)


# ---------------------------------------------------------------------------
# FSM — старт добавления
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add", IsAdmin())
async def cb_source_add_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await state.set_state(AddSourceStates.waiting_name)
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel")]
    ])
    await _safe_edit(
        query,
        "Dobavlenie istochnika (1/4)\n\nVvedite nazvanie istochnika (napr: \"Spotify Bot\"):",
        markup,
    )


# ---------------------------------------------------------------------------
# FSM — шаг 1: имя
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_name, IsAdmin())
async def fsm_waiting_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip() if message.text else ""
    if not name:
        await message.answer(
            "Nazvanie ne mozhet byt pustym. Poprobuite snova:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel")]
            ]),
        )
        return

    await state.update_data(name=name)
    await state.set_state(AddSourceStates.waiting_username)
    await message.answer(
        "Dobavlenie istochnika (2/4)\n\nVvedite @username bota-istochnika (bez @):",
        reply_markup=_fsm_nav_markup("admin:src:add:back:name"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 2: username
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_username, IsAdmin())
async def fsm_waiting_username(message: Message, state: FSMContext) -> None:
    username = (message.text or "").strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z0-9_]{3,64}", username):
        await message.answer(
            "Nekorektny username. Ispolzuyte latinskie bukvy, tsifry i _. Poprobuite snova:",
            reply_markup=_fsm_nav_markup("admin:src:add:back:name"),
        )
        return

    await state.update_data(username=username)
    await state.set_state(AddSourceStates.waiting_priority)
    await message.answer(
        "Dobavlenie istochnika (3/4)\n\nVvedite prioritet (chislo 1-100, vyshe = vazhnee):",
        reply_markup=_fsm_nav_markup("admin:src:add:back:username"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 3: приоритет
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_priority, IsAdmin())
async def fsm_waiting_priority(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= 100):
        await message.answer(
            "Nekorektny prioritet. Vvedite tseloe chislo ot 1 do 100:",
            reply_markup=_fsm_nav_markup("admin:src:add:back:username"),
        )
        return

    await state.update_data(priority=int(raw))
    await state.set_state(AddSourceStates.waiting_timeout)
    await message.answer(
        "Dobavlenie istochnika (4/4)\n\nVvedite timeout v sekundakh (10-120):",
        reply_markup=_fsm_nav_markup("admin:src:add:back:priority"),
    )


# ---------------------------------------------------------------------------
# FSM — шаг 4: таймаут → предпросмотр
# ---------------------------------------------------------------------------


@router.message(AddSourceStates.waiting_timeout, IsAdmin())
async def fsm_waiting_timeout(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (10 <= int(raw) <= 120):
        await message.answer(
            "Nekorektny timeout. Vvedite tseloe chislo ot 10 do 120:",
            reply_markup=_fsm_nav_markup("admin:src:add:back:priority"),
        )
        return

    await state.update_data(timeout=int(raw))
    await state.set_state(AddSourceStates.preview)
    data = await state.get_data()
    await message.answer(
        _preview_text(data),
        reply_markup=_fsm_confirm_markup(),
    )


# ---------------------------------------------------------------------------
# FSM — Back-навигация (callback)
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add:back:name", IsAdmin())
async def fsm_back_to_name(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_name)
    data = await state.get_data()
    hint = ' (tekushchee: "{val}")'.format(val=data["name"]) if data.get("name") else ""
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="[X] Otmenit", callback_data="admin:src:add:cancel")]
    ])
    await _safe_edit(
        query,
        "Dobavlenie istochnika (1/4)\n\nVvedite nazvanie istochnika{hint}:".format(hint=hint),
        markup,
    )


@router.callback_query(lambda c: c.data == "admin:src:add:back:username", IsAdmin())
async def fsm_back_to_username(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_username)
    data = await state.get_data()
    hint = " (tekushchee: @{val})".format(val=data["username"]) if data.get("username") else ""
    await _safe_edit(
        query,
        "Dobavlenie istochnika (2/4)\n\nVvedite @username bota-istochnika (bez @){hint}:".format(hint=hint),
        _fsm_nav_markup("admin:src:add:back:name"),
    )


@router.callback_query(lambda c: c.data == "admin:src:add:back:priority", IsAdmin())
async def fsm_back_to_priority(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddSourceStates.waiting_priority)
    data = await state.get_data()
    hint = " (tekushchee: {val})".format(val=data["priority"]) if data.get("priority") else ""
    await _safe_edit(
        query,
        "Dobavlenie istochnika (3/4)\n\nVvedite prioritet (1-100, vyshe = vazhnee){hint}:".format(hint=hint),
        _fsm_nav_markup("admin:src:add:back:username"),
    )


# ---------------------------------------------------------------------------
# FSM — отмена (callback и /cancel)
# ---------------------------------------------------------------------------


async def _fsm_cancel_to_list(state: FSMContext) -> tuple[str, InlineKeyboardMarkup]:
    """Sbros FSM, vozvrashchaet (text, markup) dlya spiska."""
    await state.clear()
    async with async_session_factory() as session:
        repo    = SourceRepository(session)
        sources = await repo.get_all()
    count  = len(sources)
    text   = "=== Istochniki muzyki ({}) ===\n\nVyberite istochnik dlya upravleniya:".format(count)
    markup = _sources_markup(sources)
    return text, markup


@router.callback_query(lambda c: c.data == "admin:src:add:cancel", IsAdmin())
async def fsm_cancel_callback(query: CallbackQuery, state: FSMContext) -> None:
    text, markup = await _fsm_cancel_to_list(state)
    await _safe_edit(query, text, markup)


@router.message(Command("cancel"), IsAdmin())
async def fsm_cancel_command(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current is None:
        await message.answer("Nichego ne otmenyaem.")
        return
    text, markup = await _fsm_cancel_to_list(state)
    await message.answer(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# FSM — подтверждение создания
# ---------------------------------------------------------------------------


@router.callback_query(lambda c: c.data == "admin:src:add:confirm", IsAdmin())
async def fsm_confirm_create(query: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()

    from infrastructure.database.models import Source

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

    await query.answer("Istochnik dobavlen!")

    async with async_session_factory() as session:
        repo = SourceRepository(session)
        src  = await repo.get_by_id(src_id)

    if src:
        await _safe_edit(query, _detail_text(src), _detail_markup(src.id, src.enabled))
    else:
        async with async_session_factory() as session:
            repo    = SourceRepository(session)
            sources = await repo.get_all()
        count  = len(sources)
        text   = "=== Istochniki muzyki ({}) ===\n\nVyberite istochnik dlya upravleniya:".format(count)
        markup = _sources_markup(sources)
        await _safe_edit(query, text, markup)
