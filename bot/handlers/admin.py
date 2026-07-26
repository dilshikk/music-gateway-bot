import logging
import os

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

_BACK_BTN = [[InlineKeyboardButton(text="Nazad", callback_data="admin:back")]]

# Log file sits at project root (two levels above bot/handlers/admin.py)
_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bot.log",
)


async def _safe_edit(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Edit message, silently ignoring 'message is not modified' on double-tap."""
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _is_ub_detail(data: str) -> bool:
    parts = data.split(":")
    return len(parts) == 3 and parts[0] == "admin" and parts[1] == "ub" and parts[2].isdigit()


def _is_user_detail(data: str) -> bool:
    parts = data.split(":")
    return len(parts) == 3 and parts[0] == "admin" and parts[1] == "user" and parts[2].isdigit()


# ── FSM States ────────────────────────────────────────────────────────────────

class AddUserbotStates(StatesGroup):
    waiting_phone = State()
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_session = State()


class BroadcastStates(StatesGroup):
    waiting_photo   = State()
    waiting_caption = State()
    waiting_link    = State()
    preview         = State()


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, pool: UserbotPool, queue: QueueManager) -> None:
    pool_stats  = pool.get_stats()
    queue_stats = queue.get_stats()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Userbots",   callback_data="admin:userbots"),
            InlineKeyboardButton(text="Istochniki", callback_data="admin:sources"),
        ],
        [
            InlineKeyboardButton(text="Kanaly",      callback_data="admin:channels"),
            InlineKeyboardButton(text="Polzovateli", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton(text="Statistika", callback_data="admin:stats"),
            InlineKeyboardButton(text="Logi",        callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton(text="Rassylka", callback_data="admin:broadcast"),
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


# ── Userbots ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:userbots", IsAdmin())
async def admin_userbots(callback: CallbackQuery, pool: UserbotPool) -> None:
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
        InlineKeyboardButton(text="<< Nazad",    callback_data="admin:back"),
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
async def admin_userbot_detail(callback: CallbackQuery, pool: UserbotPool) -> None:
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
        "Segodnya: {today}/{limit}\n"
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
            InlineKeyboardButton(text="[ON]  Vkl",  callback_data="admin:ub:enable:{id}".format(id=ub_id)),
            InlineKeyboardButton(text="[OFF] Vykl", callback_data="admin:ub:disable:{id}".format(id=ub_id)),
        ],
        [
            InlineKeyboardButton(text="[R] Restart", callback_data="admin:ub:restart:{id}".format(id=ub_id)),
            InlineKeyboardButton(text="[X] Udalit",  callback_data="admin:ub:delete:{id}".format(id=ub_id)),
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
    # BUG FIX: Python 3.10 does not allow quotes inside f-string expressions.
    # Use a plain variable instead of f"{expr_with_quotes}".
    status_text = "Userbot #{id} perezapushchen".format(id=ub_id) if ok \
        else "Oshibka perezapuska userbot #{id}".format(id=ub_id)
    await callback.message.answer(status_text)


@router.callback_query(F.data.startswith("admin:ub:delete:"), IsAdmin())
async def admin_ub_delete(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.remove_userbot(ub_id)
    await callback.answer("Userbot #{id} udalyon".format(id=ub_id), show_alert=True)
    await admin_userbots(callback, pool)


# ── Add Userbot FSM ───────────────────────────────────────────────────────────

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
async def admin_ub_add_session(message: Message, state: FSMContext, pool: UserbotPool) -> None:
    data = await state.get_data()
    await state.clear()
    session_str = message.text.strip() if message.text else ""
    if not session_str:
        await message.answer("Session ne mozhet byt pustym.")
        return
    # BUG FIX: use session_factory pattern, not a single closed session
    from infrastructure.database.repositories.userbot_repo import UserbotRepository
    from infrastructure.database.session import async_session_factory
    repo = UserbotRepository(session_factory=async_session_factory)
    existing = await repo.get_by_phone(data["phone"])
    if existing:
        await message.answer(
            "Userbot s nomerom {phone} uzhe sushchestvuet (#{eid}).\n"
            "Udalite ego snachala.".format(phone=data["phone"], eid=existing.id)
        )
        return
    userbot = await repo.create(
        phone=data["phone"], api_id=data["api_id"],
        api_hash=data["api_hash"], session_string=session_str,
    )
    ok = await pool.add_userbot(userbot.id)
    if ok:
        await message.answer("Userbot #{id} dobavlen i zapushchen!".format(id=userbot.id))
    else:
        await message.answer(
            "Userbot #{id} sokhranen, no ne zapustilsya. "
            "Proverite session_string.".format(id=userbot.id)
        )


# ── Sources ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:sources", IsAdmin())
async def admin_sources(callback: CallbackQuery) -> None:
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as session:
        repo = SourceRepository(session)
        await repo.get_or_create_vk()
        sources = await repo.get_all()
    builder = InlineKeyboardBuilder()
    for src in sources:
        status_label = "ON" if src.enabled else "OFF"
        action = "disable" if src.enabled else "enable"
        builder.row(InlineKeyboardButton(
            text="[{s}] {name} | avg:{avg}ms ok:{ok} err:{err}".format(
                s=status_label,
                name=src.name,
                avg=int(src.avg_response_ms),
                ok=src.success_count,
                err=src.error_count,
            ),
            callback_data="admin:src:{action}:{id}".format(action=action, id=src.id),
        ))
    builder.row(InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"))
    lines = ["=== Istochniki muzyki ===\n"]
    for src in sources:
        status_label = "ON" if src.enabled else "OFF"
        lines.append(
            "[{s}] {name} (@{uname})\n"
            "   Prioritet: {p} | Timeout: {t}s\n"
            "   OK: {ok} | ERR: {err} | avg: {avg}ms\n".format(
                s=status_label,
                name=src.name,
                uname=src.bot_username,
                p=src.priority,
                t=src.timeout,
                ok=src.success_count,
                err=src.error_count,
                avg=int(src.avg_response_ms),
            )
        )
    if not sources:
        lines.append("Net nastroennykh istochnikov.")
    await _safe_edit(callback, "\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(F.data.startswith("admin:src:enable:"), IsAdmin())
async def admin_src_enable(callback: CallbackQuery) -> None:
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await SourceRepository(s).set_enabled(int(callback.data.split(":")[-1]), True)
    await callback.answer("Istochnik vklyuchen", show_alert=True)
    await admin_sources(callback)

@router.callback_query(F.data.startswith("admin:src:disable:"), IsAdmin())
async def admin_src_disable(callback: CallbackQuery) -> None:
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await SourceRepository(s).set_enabled(int(callback.data.split(":")[-1]), False)
    await callback.answer("Istochnik otklyuchen", show_alert=True)
    await admin_sources(callback)


# ── Users ─────────────────────────────────────────────────────────────────────

_USERS_PAGE_SIZE = 10

@router.callback_query(F.data == "admin:users", IsAdmin())
async def admin_users(callback: CallbackQuery) -> None:
    await _show_users_page(callback, 0)

@router.callback_query(F.data.startswith("admin:users:page:"), IsAdmin())
async def admin_users_page(callback: CallbackQuery) -> None:
    await _show_users_page(callback, int(callback.data.split(":")[-1]))

async def _show_users_page(callback: CallbackQuery, page: int) -> None:
    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select, func
    from infrastructure.database.models import User
    async with async_session_factory() as session:
        total = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        users = list((await session.execute(
            select(User).order_by(User.created_at.desc())
            .offset(page * _USERS_PAGE_SIZE).limit(_USERS_PAGE_SIZE)
        )).scalars())
    builder = InlineKeyboardBuilder()
    for u in users:
        name = u.first_name or u.username or str(u.telegram_id)
        banned_label = "[BAN]" if u.is_banned else "[OK]"
        builder.row(InlineKeyboardButton(
            text="{b} {name} | req:{req}".format(
                b=banned_label, name=name[:20], req=u.total_requests
            ),
            callback_data="admin:user:{id}".format(id=u.id),
        ))
    pages = max(1, (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="<<", callback_data="admin:users:page:{p}".format(p=page - 1)))
    nav.append(InlineKeyboardButton(text="{cur}/{total}".format(cur=page + 1, total=pages), callback_data="noop"))
    if (page + 1) < pages:
        nav.append(InlineKeyboardButton(text=">>", callback_data="admin:users:page:{p}".format(p=page + 1)))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="<< Nazad", callback_data="admin:back"))
    await _safe_edit(
        callback,
        "=== Polzovateli ({total}) ===\nStranitsa {cur}/{pages}:".format(
            total=total, cur=page + 1, pages=pages
        ),
        reply_markup=builder.as_markup(),
    )
    await callback.answer()

@router.callback_query(F.data.func(_is_user_detail), IsAdmin())
async def admin_user_detail(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        user = await UserRepository(s).get_by_id(user_id)
    if not user:
        await callback.answer("Ne naydeno", show_alert=True)
        return
    name     = user.first_name or user.username or "—"
    username = "@{u}".format(u=user.username) if user.username else "—"
    status   = "[BAN]" if user.is_banned else "[OK]"
    premium  = "yes" if user.premium else "no"
    text = (
        "=== Polzovatel #{id} ===\n\n"
        "Imya: {name}\n"
        "Username: {uname}\n"
        "Telegram ID: {tg_id}\n"
        "Yazyk: {lang} | Premium: {prem}\n"
        "Status: {status}\n"
        "Zaprosov: {total} (segodnya {today})\n"
        "Registratsiya: {created}\n"
    ).format(
        id=user.id,
        name=name,
        uname=username,
        tg_id=user.telegram_id,
        lang=user.language.value,
        prem=premium,
        status=status,
        total=user.total_requests,
        today=user.daily_requests,
        created=user.created_at.strftime("%d.%m.%Y %H:%M"),
    )
    if user.is_banned and user.ban_reason:
        text += "Prichina bana: {r}\n".format(r=user.ban_reason)
    if user.is_banned:
        ban_btn = InlineKeyboardButton(
            text="[UN] Razbanit",
            callback_data="admin:user:unban:{id}".format(id=user_id),
        )
    else:
        ban_btn = InlineKeyboardButton(
            text="[BAN] Zabanit",
            callback_data="admin:user:ban:{id}".format(id=user_id),
        )
    await _safe_edit(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [ban_btn],
        [InlineKeyboardButton(text="<< Nazad", callback_data="admin:users")],
    ]))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:user:ban:"), IsAdmin())
async def admin_user_ban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await UserRepository(s).ban(user_id)
    await callback.answer("Zabanen", show_alert=True)
    callback.data = "admin:user:{id}".format(id=user_id)
    await admin_user_detail(callback)

@router.callback_query(F.data.startswith("admin:user:unban:"), IsAdmin())
async def admin_user_unban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await UserRepository(s).unban(user_id)
    await callback.answer("Razbanen", show_alert=True)
    callback.data = "admin:user:{id}".format(id=user_id)
    await admin_user_detail(callback)


# ── Channels ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels", IsAdmin())
async def admin_channels(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        "=== Kanaly ===\n\nFunktsiya v razrabotke.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats", IsAdmin())
async def admin_stats(
    callback: CallbackQuery,
    pool: UserbotPool,
    queue: QueueManager,
    cache: CacheManager,
) -> None:
    import psutil
    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select, func
    from infrastructure.database.models import User, Search
    ps = pool.get_stats()
    qs = queue.get_stats()
    redis_ok = await cache.ping()
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()
    popular = await cache.get_popular(limit=5)
    popular_text = "\n".join(
        "  {n}. {q} ({c})".format(n=i + 1, q=q, c=int(c))
        for i, (q, c) in enumerate(popular)
    ) or "  —"
    async with async_session_factory() as session:
        total_users    = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        total_searches = (await session.execute(select(func.count()).select_from(Search))).scalar_one()
    redis_label = "OK" if redis_ok else "FAIL"
    await _safe_edit(
        callback,
        "=== Statistika ===\n\n"
        "Userbots: idle={idle} busy={busy} fw={flood} err={err} off={dis}\n"
        "Ochered: {q_size} / processing={proc}\n\n"
        "Polzovateley: {users}\n"
        "Poiskov: {searches}\n\n"
        "CPU: {cpu}% | RAM: {ram_pct}% ({ram_mb} MB) | Redis: {redis}\n\n"
        "Top zaprosov:\n{popular}".format(
            idle=ps["idle"],
            busy=ps["busy"],
            flood=ps["flood"],
            err=ps["error"],
            dis=ps["disabled"],
            q_size=qs.get("queue_size", 0),
            proc=qs.get("processing", 0),
            users=total_users,
            searches=total_searches,
            cpu=cpu,
            ram_pct=ram.percent,
            ram_mb=ram.used // 1024 // 1024,
            redis=redis_label,
            popular=popular_text,
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="<< Nazad",    callback_data="admin:back"),
            InlineKeyboardButton(text="[R] Obnovit", callback_data="admin:stats"),
        ]]),
    )
    await callback.answer()


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:logs", IsAdmin())
async def admin_logs(callback: CallbackQuery) -> None:
    if not os.path.exists(_LOG_PATH):
        await _safe_edit(
            callback,
            "Fayl logov ne nayden.\n{path}\n"
            "Perezapustite bot — fayl sozdastsa avtomaticheski.".format(path=_LOG_PATH),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
        )
        await callback.answer()
        return
    with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    last = "".join(lines[-40:]).strip()
    if len(last) > 3500:
        last = "..." + last[-3500:]
    await _safe_edit(
        callback,
        "=== Logi ({n} str.) ===\n\n<pre>{log}</pre>".format(n=len(lines), log=last),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="[R] Obnovit", callback_data="admin:logs"),
            InlineKeyboardButton(text="<< Nazad",    callback_data="admin:back"),
        ]]),
    )
    await callback.answer()


# ── Broadcast FSM ─────────────────────────────────────────────────────────────

_BC_CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="[OK] Otpravit", callback_data="bc:send"),
        InlineKeyboardButton(text="[X]  Otmenit",  callback_data="bc:cancel"),
    ]
])


def _bc_link_kb(url: str, title: str) -> InlineKeyboardMarkup | None:
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title or "Perejti", url=url)]
    ])


@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_photo)
    await callback.message.answer(
        "Sozdanie rassylki (1/3)\n\n"
        "Otpravte foto dlya rassylki.\n"
        "Ili napishite /skip chtoby propustit foto.\n\n"
        "Otmena: /cancel"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_photo, IsAdmin())
async def bc_got_photo(message: Message, state: FSMContext) -> None:
    if message.text and message.text.strip().lower() in ("/skip", "skip"):
        await state.update_data(photo_id=None)
    elif message.photo:
        await state.update_data(photo_id=message.photo[-1].file_id)
    else:
        await message.answer("Otpravte foto ili /skip:")
        return
    await state.set_state(BroadcastStates.waiting_caption)
    await message.answer(
        "Sozdanie rassylki (2/3)\n\n"
        "Napishite tekst rassylki.\n"
        "Podderzhivaetsya HTML-formatirovanie.\n\n"
        "Otmena: /cancel"
    )


@router.message(BroadcastStates.waiting_caption, IsAdmin())
async def bc_got_caption(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("Tekst ne mozhet byt pustym:")
        return
    await state.update_data(caption=text)
    await state.set_state(BroadcastStates.waiting_link)
    await message.answer(
        "Sozdanie rassylki (3/3)\n\n"
        "Otpravte ssylku knopki v formate:\n"
        "Nazvanie knopki|https://example.com\n\n"
        "Ili /skip chtoby ne dobavlyat knopku.\n\n"
        "Otmena: /cancel"
    )


@router.message(BroadcastStates.waiting_link, IsAdmin())
async def bc_got_link(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    link_url   = ""
    link_title = ""
    if raw.lower() not in ("/skip", "skip"):
        if "|" in raw:
            parts      = raw.split("|", 1)
            link_title = parts[0].strip()
            link_url   = parts[1].strip()
        else:
            link_url   = raw
            link_title = "Perejti"
        if link_url and not link_url.startswith(("http://", "https://", "tg://")):
            await message.answer("Neverniy URL. Dolzhen nachinatsa s http:// ili https://\nIli /skip:")
            return
    await state.update_data(link_url=link_url, link_title=link_title)
    await state.set_state(BroadcastStates.preview)
    data = await state.get_data()
    await _send_bc_preview(message, data)


async def _send_bc_preview(message: Message, data: dict) -> None:
    photo_id   = data.get("photo_id")
    caption    = data.get("caption", "")
    link_url   = data.get("link_url", "")
    link_title = data.get("link_title", "Perejti")
    msg_kb = _bc_link_kb(link_url, link_title)
    header = "=== Predprosmotr rassylki ===\n\n"
    if photo_id:
        await message.answer_photo(photo=photo_id, caption=header + caption, reply_markup=msg_kb)
    else:
        await message.answer(header + caption, reply_markup=msg_kb)
    await message.answer("Tak budet vyglyadet rassylka. Otpravit?", reply_markup=_BC_CONFIRM_KB)


@router.callback_query(F.data == "bc:cancel", IsAdmin())
async def bc_cancel_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("Rassylka otmenena.")
    await callback.answer()


@router.callback_query(F.data == "bc:send", IsAdmin())
async def bc_do_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text("Otpravlyayu rassylku...")
    await callback.answer()
    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select
    from infrastructure.database.models import User
    photo_id   = data.get("photo_id")
    caption    = data.get("caption", "")
    link_url   = data.get("link_url", "")
    link_title = data.get("link_title", "Perejti")
    msg_kb     = _bc_link_kb(link_url, link_title)
    async with async_session_factory() as session:
        res      = await session.execute(
            select(User.telegram_id).where(User.is_banned == False)  # noqa: E712
        )
        user_ids = [row[0] for row in res.all()]
    sent = failed = 0
    for tg_id in user_ids:
        try:
            if photo_id:
                await callback.bot.send_photo(tg_id, photo=photo_id, caption=caption, reply_markup=msg_kb)
            else:
                await callback.bot.send_message(tg_id, caption, reply_markup=msg_kb)
            sent += 1
        except Exception:
            failed += 1
    await callback.message.edit_text(
        "=== Rassylka zavershena ===\n\n"
        "Otpravleno: {sent}\n"
        "Ne dostavleno: {failed}".format(sent=sent, failed=failed)
    )


# ── Cancel command ────────────────────────────────────────────────────────────

@router.message(Command("cancel"), IsAdmin())
async def admin_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await state.clear()
        await message.answer("Otmeneno.")
    else:
        await message.answer("Nechego otmenyat.")


# ── Back ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:back", IsAdmin())
async def admin_back(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager) -> None:
    await cmd_admin(callback.message, pool, queue)  # type: ignore[arg-type]
    await callback.answer()
