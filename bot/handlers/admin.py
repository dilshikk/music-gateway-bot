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

_BACK_BTN = [[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]

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
    waiting_photo   = State()   # шаг 1: фото (или /skip)
    waiting_caption = State()   # шаг 2: текст / подпись
    waiting_link    = State()   # шаг 3: ссылка «Название|https://…» (или /skip)
    preview         = State()   # шаг 4: предпросмотр → подтвердить / отменить


# ── Главное меню ─────────────────────────────────────────────────────────────

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, pool: UserbotPool, queue: QueueManager) -> None:
    pool_stats  = pool.get_stats()
    queue_stats = queue.get_stats()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Userbot'ы", callback_data="admin:userbots"),
            InlineKeyboardButton(text="📡 Источники",  callback_data="admin:sources"),
        ],
        [
            InlineKeyboardButton(text="📢 Каналы",   callback_data="admin:channels"),
            InlineKeyboardButton(text="👥 Пользов.",  callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
            InlineKeyboardButton(text="📋 Логи",      callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast"),
        ],
    ])

    await message.answer(
        " 🛠 Панель администратора \n\n"
        f"🤖 Userbot: {pool_stats['idle']} свободных / {pool_stats['total']} всего\n"
        f"⏳ Очередь: {queue_stats.get('queue_size', 0)} запросов\n"
        f"🔴 FloodWait: {pool_stats['flood']}\n"
        f"❌ Ошибок: {pool_stats['error']}",
        reply_markup=keyboard,
    )


# ── Userbots ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:userbots", IsAdmin())
async def admin_userbots(callback: CallbackQuery, pool: UserbotPool) -> None:
    entries = pool.list_userbots()
    builder = InlineKeyboardBuilder()
    for entry in entries:
        status_icon = {
            "idle": "🟢", "busy": "🟡", "flood_wait": "🔴",
            "error": "❌", "disabled": "⚫", "offline": "🔘",
        }.get(entry.model.status.value, "❓")
        builder.row(InlineKeyboardButton(
            text=f"{status_icon} #{entry.id} {entry.model.phone}",
            callback_data=f"admin:ub:{entry.id}",
        ))
    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="admin:ub:add"),
        InlineKeyboardButton(text="🔙 Назад",    callback_data="admin:back"),
    )
    await _safe_edit(callback, f" 🤖 Userbot'ы ({len(entries)} шт.)\n\nВыберите для управления:",
                     reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.func(_is_ub_detail), IsAdmin())
async def admin_userbot_detail(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    entry = next((e for e in pool.list_userbots() if e.id == ub_id), None)
    if not entry:
        await callback.answer("❌ Не найдено", show_alert=True)
        return
    m = entry.model
    text = (
        f" Userbot #{m.id} \n\n"
        f"📱 Телефон: {m.phone}\n"
        f"📊 Статус: {m.status.value}\n"
        f"⚖️ Вес: {m.weight}\n"
        f"📈 Сегодня: {m.requests_today}/{m.daily_limit}\n"
        f"📊 Всего: {m.requests_total}\n"
        f"⚠️ Ошибок: {m.error_count}\n"
    )
    if m.flood_wait_until:
        text += f"🚫 FloodWait до: {m.flood_wait_until.strftime('%H:%M:%S')}\n"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Включить",  callback_data=f"admin:ub:enable:{ub_id}"),
            InlineKeyboardButton(text="⛔ Выключить", callback_data=f"admin:ub:disable:{ub_id}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапуск",  callback_data=f"admin:ub:restart:{ub_id}"),
            InlineKeyboardButton(text="🗑 Удалить",    callback_data=f"admin:ub:delete:{ub_id}"),
        ],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:userbots")],
    ])
    await _safe_edit(callback, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:ub:enable:"), IsAdmin())
async def admin_ub_enable(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.enable_userbot(ub_id)
    await callback.answer(f"✅ Userbot #{ub_id} включён", show_alert=True)


@router.callback_query(F.data.startswith("admin:ub:disable:"), IsAdmin())
async def admin_ub_disable(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.disable_userbot(ub_id)
    await callback.answer(f"⛔ Userbot #{ub_id} выключен", show_alert=True)


@router.callback_query(F.data.startswith("admin:ub:restart:"), IsAdmin())
async def admin_ub_restart(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await callback.answer("🔄 Перезапускаю...")
    ok = await pool.restart_userbot(ub_id)
    await callback.message.answer(
        f"{"✅ Перезапущен" if ok else "❌ Ошибка"}: Userbot #{ub_id}"
    )


@router.callback_query(F.data.startswith("admin:ub:delete:"), IsAdmin())
async def admin_ub_delete(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.remove_userbot(ub_id)
    await callback.answer(f"🗑 Userbot #{ub_id} удалён", show_alert=True)
    await admin_userbots(callback, pool)


# ── Add Userbot FSM ───────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:ub:add", IsAdmin())
async def admin_ub_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddUserbotStates.waiting_phone)
    await callback.message.answer("📱 Введите номер телефона (формат: +79991234567):")
    await callback.answer()

@router.message(AddUserbotStates.waiting_phone, IsAdmin())
async def admin_ub_add_phone(message: Message, state: FSMContext) -> None:
    await state.update_data(phone=message.text.strip())
    await state.set_state(AddUserbotStates.waiting_api_id)
    await message.answer("🔑 Введите api_id (число):")

@router.message(AddUserbotStates.waiting_api_id, IsAdmin())
async def admin_ub_add_api_id(message: Message, state: FSMContext) -> None:
    if not message.text or not message.text.strip().isdigit():
        await message.answer("❌ api_id должен быть числом. Попробуйте ещё раз:")
        return
    await state.update_data(api_id=int(message.text.strip()))
    await state.set_state(AddUserbotStates.waiting_api_hash)
    await message.answer("🔑 Введите api_hash:")

@router.message(AddUserbotStates.waiting_api_hash, IsAdmin())
async def admin_ub_add_api_hash(message: Message, state: FSMContext) -> None:
    await state.update_data(api_hash=message.text.strip())
    await state.set_state(AddUserbotStates.waiting_session)
    await message.answer("📋 Введите session_string:")

@router.message(AddUserbotStates.waiting_session, IsAdmin())
async def admin_ub_add_session(message: Message, state: FSMContext, pool: UserbotPool) -> None:
    data = await state.get_data()
    await state.clear()
    session = message.text.strip() if message.text else ""
    if not session:
        await message.answer("❌ Session не может быть пустым.")
        return
    from infrastructure.database.repositories.userbot_repo import UserbotRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as db:
        repo = UserbotRepository(db)
        existing = await repo.get_by_phone(data["phone"])
        if existing:
            await message.answer(
                f"⚠️ Userbot с номером {data['phone']} уже существует (#{existing.id}).\n"
                "Удалите его сначала."
            )
            return
        userbot = await repo.create(
            phone=data["phone"], api_id=data["api_id"],
            api_hash=data["api_hash"], session_string=session,
        )
    ok = await pool.add_userbot(userbot.id)
    await message.answer(
        f"✅ Userbot #{userbot.id} добавлен и запущен!" if ok
        else f"⚠️ Userbot #{userbot.id} сохранён, но не запустился. Проверьте session_string."
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
        icon   = "🟢" if src.enabled else "🔴"
        action = "disable" if src.enabled else "enable"
        builder.row(InlineKeyboardButton(
            text=f"{icon} {src.name}  |⏱{int(src.avg_response_ms)}ms  ✅{src.success_count} ❌{src.error_count}",
            callback_data=f"admin:src:{action}:{src.id}",
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"))
    lines = [" 📡 Источники музыки \n"]
    for src in sources:
        icon = "🟢" if src.enabled else "🔴"
        lines.append(
            f"{icon} <b>{src.name}</b> (@{src.bot_username})\n"
            f"   Приоритет: {src.priority} | Таймаут: {src.timeout}с\n"
            f"   Успешно: {src.success_count} | Ошибок: {src.error_count}\n"
            f"   Ср. время: {int(src.avg_response_ms)} мс\n"
        )
    if not sources:
        lines.append("Нет настроенных источников.")
    await _safe_edit(callback, "\n".join(lines), reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(F.data.startswith("admin:src:enable:"), IsAdmin())
async def admin_src_enable(callback: CallbackQuery) -> None:
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await SourceRepository(s).set_enabled(int(callback.data.split(":")[-1]), True)
    await callback.answer("✅ Источник включён", show_alert=True)
    await admin_sources(callback)

@router.callback_query(F.data.startswith("admin:src:disable:"), IsAdmin())
async def admin_src_disable(callback: CallbackQuery) -> None:
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await SourceRepository(s).set_enabled(int(callback.data.split(":")[-1]), False)
    await callback.answer("⛔ Источник отключён", show_alert=True)
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
        builder.row(InlineKeyboardButton(
            text=f"{"🚫" if u.is_banned else "👤"} {name[:20]} | req:{u.total_requests}",
            callback_data=f"admin:user:{u.id}",
        ))
    pages = max(1, (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:users:page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{pages}", callback_data="noop"))
    if (page + 1) < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:users:page:{page+1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"))
    await _safe_edit(callback, f" 👥 Пользователи ({total})\nСтраница {page+1}/{pages}:",
                     reply_markup=builder.as_markup())
    await callback.answer()

@router.callback_query(F.data.func(_is_user_detail), IsAdmin())
async def admin_user_detail(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        user = await UserRepository(s).get_by_id(user_id)
    if not user:
        await callback.answer("❌ Не найден", show_alert=True)
        return
    name     = user.first_name or user.username or "—"
    username = f"@{user.username}" if user.username else "—"
    status   = "🚫 Заблокирован" if user.is_banned else "✅ Активен"
    text = (
        f" 👤 Пользователь #{user.id} \n\n"
        f"Имя: {name}\nUsername: {username}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Язык: {user.language.value} | Premium: {'✅' if user.premium else '❌'}\n"
        f"Статус: {status}\n"
        f"Запросов: {user.total_requests} (сегодня {user.daily_requests})\n"
        f"Регистрация: {user.created_at.strftime('%d.%m.%Y %H:%M')}\n"
    )
    if user.is_banned and user.ban_reason:
        text += f"Причина бана: {user.ban_reason}\n"
    ban_btn = (
        InlineKeyboardButton(text="✅ Разбанить", callback_data=f"admin:user:unban:{user_id}")
        if user.is_banned
        else InlineKeyboardButton(text="🚫 Забанить", callback_data=f"admin:user:ban:{user_id}")
    )
    await _safe_edit(callback, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [ban_btn],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:users")],
    ]))
    await callback.answer()

@router.callback_query(F.data.startswith("admin:user:ban:"), IsAdmin())
async def admin_user_ban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await UserRepository(s).ban(user_id)
    await callback.answer("🚫 Заблокирован", show_alert=True)
    callback.data = f"admin:user:{user_id}"
    await admin_user_detail(callback)

@router.callback_query(F.data.startswith("admin:user:unban:"), IsAdmin())
async def admin_user_unban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as s:
        await UserRepository(s).unban(user_id)
    await callback.answer("✅ Разблокирован", show_alert=True)
    callback.data = f"admin:user:{user_id}"
    await admin_user_detail(callback)


# ── Channels ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels", IsAdmin())
async def admin_channels(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        " 📢 Каналы \n\nФункция в разработке.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()


# ── Stats ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats", IsAdmin())
async def admin_stats(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager, cache: CacheManager) -> None:
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
    popular_text = "\n".join(f" {i+1}. {q} ({int(c)})" for i, (q, c) in enumerate(popular)) or " —"
    async with async_session_factory() as session:
        total_users   = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        total_searches = (await session.execute(select(func.count()).select_from(Search))).scalar_one()
    await _safe_edit(
        callback,
        f" 📊 Статистика \n\n"
        f"🤖 Userbots: 🟢{ps['idle']} 🟡{ps['busy']} 🔴{ps['flood']} ❌{ps['error']} ⚫{ps['disabled']}\n"
        f"⏳ Очередь: {qs.get('queue_size',0)} / ⚙️{qs.get('processing',0)}\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🔍 Поисков: {total_searches}\n\n"
        f"💻 CPU: {cpu}% | 🧠 RAM: {ram.percent}% ({ram.used//1024//1024} MB) | 📦 Redis: {'✅' if redis_ok else '❌'}\n\n"
        f"🔥 Топ запросов:\n{popular_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад",    callback_data="admin:back"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats"),
        ]]),
    )
    await callback.answer()


# ── Logs ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:logs", IsAdmin())
async def admin_logs(callback: CallbackQuery) -> None:
    if not os.path.exists(_LOG_PATH):
        await _safe_edit(
            callback,
            f"📋 Файл логов не найден.\n<code>{_LOG_PATH}</code>\nПерезапустите бот — файл создастся автоматически.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
        )
        await callback.answer()
        return
    with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    last = "".join(lines[-40:]).strip()
    if len(last) > 3500:
        last = "…" + last[-3500:]
    await _safe_edit(
        callback,
        f" 📋 Логи ({len(lines)} стр.) \n\n<pre>{last}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:logs"),
            InlineKeyboardButton(text="🔙 Назад",    callback_data="admin:back"),
        ]]),
    )
    await callback.answer()


# ── Broadcast FSM ─────────────────────────────────────────────────────────────
# Флоу: фото → текст/подпись → кнопка-ссылка → предпросмотр → отправить / отменить

_BC_CONFIRM_KB = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="✅ Отправить", callback_data="bc:send"),
        InlineKeyboardButton(text="❌ Отменить", callback_data="bc:cancel"),
    ]
])


def _bc_link_kb(url: str, title: str) -> InlineKeyboardMarkup | None:
    """Build a one-button inline keyboard for the broadcast link, or None."""
    if not url:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=title or "🔗 Перейти", url=url)]
    ])


@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_photo)
    await callback.message.answer(
        "📣 <b>Создание рассылки</b> (1/3)\n\n"
        "🖼 Отправьте <b>фото</b> для рассылки.\n"
        "Или напишите /skip чтобы пропустить фото.\n\n"
        "Отмена: /cancel"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_photo, IsAdmin())
async def bc_got_photo(message: Message, state: FSMContext) -> None:
    # Skip command
    if message.text and message.text.strip().lower() in ("/skip", "skip"):
        await state.update_data(photo_id=None)
    elif message.photo:
        await state.update_data(photo_id=message.photo[-1].file_id)
    else:
        await message.answer("❌ Отправьте фото или /skip:")
        return

    await state.set_state(BroadcastStates.waiting_caption)
    await message.answer(
        "📣 <b>Создание рассылки</b> (2/3)\n\n"
        "📝 Напишите <b>текст</b> рассылки.\n"
        "Поддерживается HTML-форматирование.\n\n"
        "Отмена: /cancel"
    )


@router.message(BroadcastStates.waiting_caption, IsAdmin())
async def bc_got_caption(message: Message, state: FSMContext) -> None:
    text = message.text or message.caption or ""
    if not text.strip():
        await message.answer("❌ Текст не может быть пустым:")
        return
    await state.update_data(caption=text)
    await state.set_state(BroadcastStates.waiting_link)
    await message.answer(
        "📣 <b>Создание рассылки</b> (3/3)\n\n"
        "🔗 Отправьте ссылку кнопки в формате:\n"
        "<code>Название кнопки|https://example.com</code>\n\n"
        "Или /skip чтобы не добавлять кнопку.\n\n"
        "Отмена: /cancel"
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
            link_title = "🔗 Перейти"

        if link_url and not link_url.startswith(("http://", "https://", "tg://")):
            await message.answer("❌ Неверный URL. Должен начинаться с http:// или https://\nИли /skip:")
            return

    await state.update_data(link_url=link_url, link_title=link_title)
    await state.set_state(BroadcastStates.preview)

    # Show preview
    data = await state.get_data()
    await _send_bc_preview(message, data)


async def _send_bc_preview(message: Message, data: dict) -> None:
    """Send the broadcast preview to admin with confirm/cancel buttons."""
    photo_id   = data.get("photo_id")
    caption    = data.get("caption", "")
    link_url   = data.get("link_url", "")
    link_title = data.get("link_title", "🔗 Перейти")

    # Build keyboard for the broadcast message itself
    msg_kb = _bc_link_kb(link_url, link_title)

    # Build confirm/cancel keyboard shown under the preview
    preview_kb = _BC_CONFIRM_KB

    header = "👁 <b>Предпросмотр рассылки</b>\n\n"

    if photo_id:
        await message.answer_photo(
            photo=photo_id,
            caption=header + caption,
            reply_markup=msg_kb,
        )
    else:
        await message.answer(header + caption, reply_markup=msg_kb)

    await message.answer(
        "ℹ️ Так будет выглядеть рассылка. Отправить?",
        reply_markup=preview_kb,
    )


@router.callback_query(F.data == "bc:cancel", IsAdmin())
async def bc_cancel_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()


@router.callback_query(F.data == "bc:send", IsAdmin())
async def bc_do_send(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    await callback.message.edit_text("⏳ Отправляю рассылку...")
    await callback.answer()

    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select
    from infrastructure.database.models import User

    photo_id   = data.get("photo_id")
    caption    = data.get("caption", "")
    link_url   = data.get("link_url", "")
    link_title = data.get("link_title", "🔗 Перейти")
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
                await callback.bot.send_photo(
                    tg_id, photo=photo_id, caption=caption, reply_markup=msg_kb
                )
            else:
                await callback.bot.send_message(tg_id, caption, reply_markup=msg_kb)
            sent += 1
        except Exception:
            failed += 1

    await callback.message.edit_text(
        f"📣 <b>Рассылка завершена</b>\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


# ── Cancel command ────────────────────────────────────────────────────────────

@router.message(Command("cancel"), IsAdmin())
async def admin_cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state():
        await state.clear()
        await message.answer("❌ Отменено.")
    else:
        await message.answer("Нечего отменять.")


# ── Back ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:back", IsAdmin())
async def admin_back(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager) -> None:
    await cmd_admin(callback.message, pool, queue)  # type: ignore[arg-type]
    await callback.answer()
