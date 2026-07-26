import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.filters.admin import IsAdmin
from core.cache_manager import CacheManager
from core.queue_manager import QueueManager
from core.userbot_pool import UserbotPool

logger = logging.getLogger(__name__)
router = Router(name="admin")

_BACK_BTN = [[InlineKeyboardButton(text="Назад", callback_data="admin:back")]]


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Edit message, ignoring 'message is not modified' errors on double-tap."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _is_ub_detail(data: str) -> bool:
    """True only for 'admin:ub:{int}' — exactly 3 colon-separated parts."""
    parts = data.split(":")
    return len(parts) == 3 and parts[2].isdigit()


# ── FSM States ────────────────────────────────────────────────────────────────

class AddUserbotStates(StatesGroup):
    waiting_phone = State()
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_session = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


# ── Главное меню админа ───────────────────────────────────────────────────────

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, pool: UserbotPool, queue: QueueManager) -> None:
    pool_stats = pool.get_stats()
    queue_stats = queue.get_stats()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="[U] Userbots", callback_data="admin:userbots"),
            InlineKeyboardButton(text="[S] Istochniki", callback_data="admin:sources"),
        ],
        [
            InlineKeyboardButton(text="[C] Kanaly", callback_data="admin:channels"),
            InlineKeyboardButton(text="[P] Polzovateli", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton(text="[Q] Statistika", callback_data="admin:stats"),
            InlineKeyboardButton(text="[L] Logi", callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton(text="[B] Rassylka", callback_data="admin:broadcast"),
        ],
    ])

    await message.answer(
        "=== Admin Panel ===\n\n"
        "Userbots: {idle} svobodnyh / {total} vsego\n"
        "Ochered: {queue_size} zaprosov\n"
        "FloodWait: {flood}\n"
        "Oshibok: {error}".format(
            idle=pool_stats["idle"],
            total=pool_stats["total"],
            queue_size=queue_stats.get("queue_size", 0),
            flood=pool_stats["flood"],
            error=pool_stats["error"],
        ),
        reply_markup=keyboard,
    )

# ── Управление Userbot ────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:userbots", IsAdmin())
async def admin_userbots(callback: CallbackQuery, pool: UserbotPool) -> None:
    entries = pool.list_userbots()
    builder = InlineKeyboardBuilder()

    STATUS_LABELS = {
        "idle":       "[OK]",
        "busy":       "[..]",
        "flood_wait": "[FW]",
        "error":      "[ER]",
        "disabled":   "[--]",
        "offline":    "[OF]",
    }

    for entry in entries:
        label = STATUS_LABELS.get(entry.model.status.value, "[?]")
        builder.row(InlineKeyboardButton(
            text="{label} #{eid} {phone}".format(
                label=label, eid=entry.id, phone=entry.model.phone
            ),
            callback_data="admin:ub:{id}".format(id=entry.id),
        ))

    builder.row(
        InlineKeyboardButton(text="[+] Dobavit", callback_data="admin:ub:add"),
        InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"),
    )

    await _safe_edit(
        callback,
        "=== Userbots ({count} sht.) ===\n\nVyberite dlya upravleniya:".format(
            count=len(entries)
        ),
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.func(_is_ub_detail), IsAdmin())
async def admin_userbot_detail(
    callback: CallbackQuery,
    pool: UserbotPool,
) -> None:
    ub_id = int(callback.data.split(":")[-1])
    entry = next((e for e in pool.list_userbots() if e.id == ub_id), None)

    if not entry:
        await callback.answer("Ne naydeno", show_alert=True)
        return

    m = entry.model
    flood_line = ""
    if m.flood_wait_until:
        flood_line = "\nFloodWait do: {t}".format(
            t=m.flood_wait_until.strftime("%H:%M:%S")
        )

    text = (
        "=== Userbot #{id} ===\n\n"
        "Telefon: {phone}\n"
        "Status: {status}\n"
        "Ves: {weight}\n"
        "Segodnya: {today} / {limit}\n"
        "Vsego: {total}\n"
        "Oshibok: {errors}{flood}"
    ).format(
        id=m.id,
        phone=m.phone,
        status=m.status.value,
        weight=m.weight,
        today=m.requests_today,
        limit=m.daily_limit,
        total=m.requests_total,
        errors=m.error_count,
        flood=flood_line,
    )

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="[ON] Vkl", callback_data="admin:ub:enable:{id}".format(id=ub_id)),
            InlineKeyboardButton(text="[OFF] Vykl", callback_data="admin:ub:disable:{id}".format(id=ub_id)),
        ],
        [
            InlineKeyboardButton(text="[R] Restart", callback_data="admin:ub:restart:{id}".format(id=ub_id)),
            InlineKeyboardButton(text="[X] Udalit", callback_data="admin:ub:delete:{id}".format(id=ub_id)),
        ],
        [InlineKeyboardButton(text="<< Nazad", callback_data="admin:userbots")],
    ])

    await _safe_edit(callback, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:ub:enable:"), IsAdmin())
async def admin_ub_enable(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.enable_userbot(ub_id)
    await callback.answer("Userbot #{id} vklyuchen".format(id=ub_id), show_alert=True)


@router.callback_query(F.data.startswith("admin:ub:disable:"), IsAdmin())
async def admin_ub_disable(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.disable_userbot(ub_id)
    await callback.answer("Userbot #{id} vyklyuchen".format(id=ub_id), show_alert=True)


@router.callback_query(F.data.startswith("admin:ub:restart:"), IsAdmin())
async def admin_ub_restart(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await callback.answer("Perezapuskayu...")
    ok = await pool.restart_userbot(ub_id)
    if ok:
        status_text = "Userbot #{id} perezapushchen".format(id=ub_id)
    else:
        status_text = "Oshibka perezapuska userbot #{id}".format(id=ub_id)
    await callback.message.answer(status_text)


@router.callback_query(F.data.startswith("admin:ub:delete:"), IsAdmin())
async def admin_ub_delete(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.remove_userbot(ub_id)
    await callback.answer("Userbot #{id} udalyon".format(id=ub_id), show_alert=True)
    entries = pool.list_userbots()
    builder = InlineKeyboardBuilder()
    STATUS_LABELS = {
        "idle": "[OK]", "busy": "[..]", "flood_wait": "[FW]",
        "error": "[ER]", "disabled": "[--]", "offline": "[OF]",
    }
    for entry in entries:
        label = STATUS_LABELS.get(entry.model.status.value, "[?]")
        builder.row(InlineKeyboardButton(
            text="{label} #{eid} {phone}".format(
                label=label, eid=entry.id, phone=entry.model.phone
            ),
            callback_data="admin:ub:{id}".format(id=entry.id),
        ))
    builder.row(
        InlineKeyboardButton(text="[+] Dobavit", callback_data="admin:ub:add"),
        InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"),
    )
    await _safe_edit(
        callback,
        "=== Userbots ({count} sht.) ===\n\nVyberite dlya upravleniya:".format(
            count=len(entries)
        ),
        reply_markup=builder.as_markup(),
    )


# ── Добавление Userbot через FSM ──────────────────────────────────────────────

@router.callback_query(F.data == "admin:ub:add", IsAdmin())
async def admin_ub_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddUserbotStates.waiting_phone)
    await callback.message.answer("Vvedite nomer telefona (format: +79991234567):")
    await callback.answer()


@router.message(AddUserbotStates.waiting_phone, IsAdmin())
async def admin_ub_add_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip())
    await state.set_state(AddUserbotStates.waiting_api_id)
    await message.answer("Vvedite api_id (chislo):")


@router.message(AddUserbotStates.waiting_api_id, IsAdmin())
async def admin_ub_add_api_id(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("api_id dolzhen byt chislom. Poprobuite eshche raz:")
        return
    await state.update_data(api_id=int(message.text.strip()))
    await state.set_state(AddUserbotStates.waiting_api_hash)
    await message.answer("Vvedite api_hash:")


@router.message(AddUserbotStates.waiting_api_hash, IsAdmin())
async def admin_ub_add_api_hash(message: Message, state: FSMContext) -> None:
    await state.update_data(api_hash=message.text.strip())
    await state.set_state(AddUserbotStates.waiting_session)
    await message.answer("Vvedite session_string (iz Pyrogram StringSession):")


@router.message(AddUserbotStates.waiting_session, IsAdmin())
async def admin_ub_add_session(
    message: Message,
    state: FSMContext,
    pool: UserbotPool,
) -> None:
    data = await state.get_data()
    await state.clear()

    session_str = message.text.strip() if message.text else ""
    if not session_str:
        await message.answer("Session ne mozhet byt pustym.")
        return

    # BUG FIX: использовать новый API репозитория — передавать фабрику,
    # а не открытую сессию, иначе сессия закроется до вызова pool.add_userbot()
    from infrastructure.database.repositories.userbot_repo import UserbotRepository
    from infrastructure.database.session import async_session_factory

    repo = UserbotRepository(session_factory=async_session_factory)

    existing = await repo.get_by_phone(data["phone"])
    if existing:
        await message.answer(
            "Userbot s nomerom {phone} uzhe sushchestvuet (#{id}).\n"
            "Udalite ego snachala ili ispolzuyte drugoy nomer.".format(
                phone=data["phone"], id=existing.id
            )
        )
        return

    userbot = await repo.create(
        phone=data["phone"],
        api_id=data["api_id"],
        api_hash=data["api_hash"],
        session_string=session_str,
    )

    ok = await pool.add_userbot(userbot.id)

    if ok:
        await message.answer(
            "Userbot #{id} dobavlen i zapushchen!".format(id=userbot.id)
        )
    else:
        await message.answer(
            "Userbot #{id} sokhranen, no ne zapustilsya. "
            "Proverite session_string.".format(id=userbot.id)
        )

# ── Статистика ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats", IsAdmin())
async def admin_stats(
    callback: CallbackQuery,
    pool: UserbotPool,
    queue: QueueManager,
    cache: CacheManager,
) -> None:
    import psutil

    pool_stats = pool.get_stats()
    queue_stats = queue.get_stats()
    redis_ok = await cache.ping()

    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()

    popular = await cache.get_popular(limit=5)
    popular_text = "\n".join(
        "  {n}. {q} ({c})".format(n=i + 1, q=q, c=int(c))
        for i, (q, c) in enumerate(popular)
    ) or "  —"

    redis_status = "OK" if redis_ok else "FAIL"

    await _safe_edit(
        callback,
        "=== Statistika ===\n\n"
        "-- Userbots --\n"
        "  Svobodnyh: {idle}\n"
        "  Zanyatyh: {busy}\n"
        "  FloodWait: {flood}\n"
        "  Oshibok: {ub_error}\n"
        "  Vyklyucheno: {disabled}\n\n"
        "-- Ochered --\n"
        "  V ocheredi: {q_size}\n"
        "  Obrabatyvaetsya: {processing}\n\n"
        "-- Sistema --\n"
        "  CPU: {cpu}%\n"
        "  RAM: {ram_pct}% ({ram_mb} MB)\n"
        "  Redis: {redis}\n\n"
        "-- Top zaprosov --\n"
        "{popular}".format(
            idle=pool_stats["idle"],
            busy=pool_stats["busy"],
            flood=pool_stats["flood"],
            ub_error=pool_stats["error"],
            disabled=pool_stats["disabled"],
            q_size=queue_stats.get("queue_size", 0),
            processing=queue_stats.get("processing", 0),
            cpu=cpu,
            ram_pct=ram.percent,
            ram_mb=ram.used // 1024 // 1024,
            redis=redis_status,
            popular=popular_text,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"),
                InlineKeyboardButton(text="[R] Obnovit", callback_data="admin:stats"),
            ]
        ]),
    )
    await callback.answer()

# ── Логи ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:logs", IsAdmin())
async def admin_logs(callback: CallbackQuery) -> None:
    import os
    log_path = "bot.log"
    text = "=== Poslednie logi ===\n\n"
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        last = "".join(lines[-30:]).strip()
        # Telegram message limit is 4096 chars
        if len(last) > 3500:
            last = "..." + last[-3500:]
        text += "<pre>{log}</pre>".format(log=last) if last else "Fayl pust."
    else:
        text += "Fayl logov ne nayden.\nUbedites chto logging nastroyen na zapis v bot.log"

    await _safe_edit(
        callback,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()

# ── Источники ─────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sources", IsAdmin())
async def admin_sources(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        "=== Istochniki muzyki ===\n\n"
        "Aktivnye istochniki:\n"
        "  VK Music -- podklyuchen\n\n"
        "Upravlenie istochnikami budet dostupno v sleduyushchey versii.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()

# ── Каналы ────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels", IsAdmin())
async def admin_channels(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        "=== Upravlenie kanalami ===\n\n"
        "Eta funktsiya nakhoditsya v razrabotke.\n"
        "Zdes budet upravlenie kanalami dlya rassylki rezultatov.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()

# ── Пользователи ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:users", IsAdmin())
async def admin_users(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        "=== Polzovateli ===\n\n"
        "Eta funktsiya nakhoditsya v razrabotke.\n"
        "Zdes budet spisok polzovateley bota i upravlenie imi.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()

# ── Рассылка ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_text)
    await callback.message.answer(
        "Vvedite tekst dlya rassylki vsem polzovatelyam.\n"
        "Dlya otmeny otpravte /cancel"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_text, IsAdmin())
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    # Placeholder — реальная рассылка требует таблицы users в БД
    await message.answer(
        "Rassylka poka ne realizovana.\n"
        "Dlya otpravki nuzhno podklyuchit tablitsu polzovateley."
    )


@router.message(Command("cancel"), IsAdmin())
async def admin_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer("Otmeneno.")
    else:
        await message.answer("Nechego otmenyat.")

# ── Назад ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:back", IsAdmin())
async def admin_back(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager) -> None:
    await cmd_admin(callback.message, pool, queue)  # type: ignore[arg-type]
    await callback.answer()
