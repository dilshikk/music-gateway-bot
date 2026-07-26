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

# Log file sits at project root (two levels above this file: bot/handlers/admin.py)
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
    """True only for 'admin:ub:{int}' — exactly 3 colon-separated parts."""
    parts = data.split(":")
    return len(parts) == 3 and parts[0] == "admin" and parts[1] == "ub" and parts[2].isdigit()


def _is_user_detail(data: str) -> bool:
    """True only for 'admin:user:{int}' — exactly 3 colon-separated parts."""
    parts = data.split(":")
    return len(parts) == 3 and parts[0] == "admin" and parts[1] == "user" and parts[2].isdigit()


# ── FSM States ───────────────────────────────────────────────────────────────────────────────

class AddUserbotStates(StatesGroup):
    waiting_phone = State()
    waiting_api_id = State()
    waiting_api_hash = State()
    waiting_session = State()


class BroadcastStates(StatesGroup):
    waiting_text = State()


# ── Главное меню админа ─────────────────────────────────────────────────────────────────────────────

@router.message(Command("admin"), IsAdmin())
async def cmd_admin(message: Message, pool: UserbotPool, queue: QueueManager) -> None:
    pool_stats = pool.get_stats()
    queue_stats = queue.get_stats()

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Userbot'ы", callback_data="admin:userbots"),
            InlineKeyboardButton(text="📡 Источники", callback_data="admin:sources"),
        ],
        [
            InlineKeyboardButton(text="📢 Каналы", callback_data="admin:channels"),
            InlineKeyboardButton(text="👥 Пользов.", callback_data="admin:users"),
        ],
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="admin:stats"),
            InlineKeyboardButton(text="📋 Логи", callback_data="admin:logs"),
        ],
        [
            InlineKeyboardButton(text="📣 Рассылка", callback_data="admin:broadcast"),
        ],
    ])

    await message.answer(
        " 🛠 Панель администратора \n\n"
        f"🤖 Userbot: {pool_stats['idle']} свободных / "
        f" {pool_stats['total']} всего\n"
        f"⏳ Очередь: {queue_stats.get('queue_size', 0)} запросов\n"
        f"🔴 FloodWait: {pool_stats['flood']} \n"
        f"❌ Ошибок: {pool_stats['error']} ",
        reply_markup=keyboard,
    )


# ── Управление Userbot ────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:userbots", IsAdmin())
async def admin_userbots(callback: CallbackQuery, pool: UserbotPool) -> None:
    entries = pool.list_userbots()
    builder = InlineKeyboardBuilder()

    for entry in entries:
        status_icon = {
            "idle": "🟢",
            "busy": "🟡",
            "flood_wait": "🔴",
            "error": "❌",
            "disabled": "⚫",
            "offline": "🔘",
        }.get(entry.model.status.value, "❓")

        builder.row(InlineKeyboardButton(
            text=f"{status_icon} #{entry.id} {entry.model.phone}",
            callback_data=f"admin:ub:{entry.id}",
        ))

    builder.row(
        InlineKeyboardButton(text="➕ Добавить", callback_data="admin:ub:add"),
        InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"),
    )

    await _safe_edit(
        callback,
        f" 🤖 Userbot'ы ({len(entries)} шт.)\n\nВыберите для управления:",
        reply_markup=builder.as_markup(),
    )
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
        f"📱 Телефон: {m.phone} \n"
        f"📊 Статус: {m.status.value} \n"
        f"⚖️ Вес: {m.weight}\n"
        f"📈 Сегодня: {m.requests_today} / {m.daily_limit}\n"
        f"📊 Всего: {m.requests_total}\n"
        f"⚠️ Ошибок: {m.error_count}\n"
    )
    if m.flood_wait_until:
        text += f"🚫 FloodWait до: {m.flood_wait_until.strftime('%H:%M:%S')}\n"

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Включить", callback_data=f"admin:ub:enable:{ub_id}"),
            InlineKeyboardButton(text="⛔ Выключить", callback_data=f"admin:ub:disable:{ub_id}"),
        ],
        [
            InlineKeyboardButton(text="🔄 Перезапуск", callback_data=f"admin:ub:restart:{ub_id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin:ub:delete:{ub_id}"),
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
    status = "✅ Перезапущен" if ok else "❌ Ошибка перезапуска"
    await callback.message.answer(f"{status}: Userbot #{ub_id}")


@router.callback_query(F.data.startswith("admin:ub:delete:"), IsAdmin())
async def admin_ub_delete(callback: CallbackQuery, pool: UserbotPool) -> None:
    ub_id = int(callback.data.split(":")[-1])
    await pool.remove_userbot(ub_id)
    await callback.answer(f"🗑 Userbot #{ub_id} удалён", show_alert=True)
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
        InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"),
    )
    await _safe_edit(
        callback,
        f" 🤖 Userbot'ы ({len(entries)} шт.)\n\nВыберите для управления:",
        reply_markup=builder.as_markup(),
    )


# ── Добавление Userbot через FSM ─────────────────────────────────────────────────────────────────────────────

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
async def admin_ub_add_session(
    message: Message,
    state: FSMContext,
    pool: UserbotPool,
) -> None:
    data = await state.get_data()
    await state.clear()

    session = message.text.strip() if message.text else ""
    if not session:
        await message.answer("❌ Session не может быть пустым.")
        return

    from infrastructure.database.repositories.userbot_repo import UserbotRepository
    from infrastructure.database.session import async_session_factory

    async with async_session_factory() as session_db:
        repo = UserbotRepository(session_db)
        existing = await repo.get_by_phone(data["phone"])
        if existing:
            await message.answer(
                f"⚠️ Userbot с номером {data['phone']} уже существует (#{existing.id}).\n"
                "Удалите его сначала или используйте другой номер."
            )
            return
        userbot = await repo.create(
            phone=data["phone"],
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            session_string=session,
        )

    ok = await pool.add_userbot(userbot.id)
    if ok:
        await message.answer(f"✅ Userbot #{userbot.id} добавлен и запущен!")
    else:
        await message.answer(
            f"⚠️ Userbot #{userbot.id} сохранён, но не запустился. "
            "Проверьте session_string."
        )


# ── Источники музыки ───────────────────────────────────────────────────────────────────────────────

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
        icon = "🟢" if src.enabled else "🔴"
        action = "disable" if src.enabled else "enable"
        builder.row(InlineKeyboardButton(
            text=f"{icon} {src.name}  |  ⏱ {int(src.avg_response_ms)}ms  |  ✅{src.success_count} ❌{src.error_count}",
            callback_data=f"admin:src:{action}:{src.id}",
        ))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"))

    text_lines = [" 📡 Источники музыки \n"]
    for src in sources:
        icon = "🟢" if src.enabled else "🔴"
        text_lines.append(
            f"{icon} <b>{src.name}</b> (@{src.bot_username})\n"
            f"   Приоритет: {src.priority} | Таймаут: {src.timeout}с\n"
            f"   Успешно: {src.success_count} | Ошибок: {src.error_count}\n"
            f"   Ср. время: {int(src.avg_response_ms)} мс\n"
        )
    if not sources:
        text_lines.append("Нет настроенных источников.")

    await _safe_edit(callback, "\n".join(text_lines), reply_markup=builder.as_markup())
    await callback.answer()


@router.callback_query(F.data.startswith("admin:src:enable:"), IsAdmin())
async def admin_src_enable(callback: CallbackQuery) -> None:
    src_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as session:
        await SourceRepository(session).set_enabled(src_id, True)
    await callback.answer("✅ Источник включён", show_alert=True)
    await admin_sources(callback)


@router.callback_query(F.data.startswith("admin:src:disable:"), IsAdmin())
async def admin_src_disable(callback: CallbackQuery) -> None:
    src_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.source_repo import SourceRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as session:
        await SourceRepository(session).set_enabled(src_id, False)
    await callback.answer("⛔ Источник отключён", show_alert=True)
    await admin_sources(callback)


# ── Пользователи ───────────────────────────────────────────────────────────────────────────────────

_USERS_PAGE_SIZE = 10


@router.callback_query(F.data == "admin:users", IsAdmin())
async def admin_users(callback: CallbackQuery) -> None:
    await _show_users_page(callback, page=0)


@router.callback_query(F.data.startswith("admin:users:page:"), IsAdmin())
async def admin_users_page(callback: CallbackQuery) -> None:
    page = int(callback.data.split(":")[-1])
    await _show_users_page(callback, page=page)


async def _show_users_page(callback: CallbackQuery, page: int) -> None:
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select, func
    from infrastructure.database.models import User

    async with async_session_factory() as session:
        offset = page * _USERS_PAGE_SIZE
        count_res = await session.execute(select(func.count()).select_from(User))
        total = count_res.scalar_one()
        res = await session.execute(
            select(User).order_by(User.created_at.desc()).offset(offset).limit(_USERS_PAGE_SIZE)
        )
        users = list(res.scalars().all())

    builder = InlineKeyboardBuilder()
    for u in users:
        ban_icon = "🚫" if u.is_banned else "👤"
        name = u.first_name or u.username or str(u.telegram_id)
        builder.row(InlineKeyboardButton(
            text=f"{ban_icon} {name[:20]} | req:{u.total_requests}",
            callback_data=f"admin:user:{u.id}",
        ))

    pages = max(1, (total + _USERS_PAGE_SIZE - 1) // _USERS_PAGE_SIZE)
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"admin:users:page:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if (page + 1) < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"admin:users:page:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"))

    await _safe_edit(
        callback,
        f" 👥 Пользователи (всего {total})\nСтраница {page + 1}/{pages}:",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.func(_is_user_detail), IsAdmin())
async def admin_user_detail(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory

    async with async_session_factory() as session:
        user = await UserRepository(session).get_by_id(user_id)

    if not user:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    name = user.first_name or user.username or "—"
    username = f"@{user.username}" if user.username else "—"
    status = "🚫 Заблокирован" if user.is_banned else "✅ Активен"
    text = (
        f" 👤 Пользователь #{user.id} \n\n"
        f"Имя: {name}\n"
        f"Username: {username}\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Язык: {user.language.value}\n"
        f"Premium: {'✅' if user.premium else '❌'}\n"
        f"Статус: {status}\n"
        f"Запросов сегодня: {user.daily_requests}\n"
        f"Запросов всего: {user.total_requests}\n"
        f"Регистрация: {user.created_at.strftime('%d.%m.%Y %H:%M')}\n"
    )
    if user.is_banned and user.ban_reason:
        text += f"Причина бана: {user.ban_reason}\n"

    ban_btn = (
        InlineKeyboardButton(text="✅ Разбанить", callback_data=f"admin:user:unban:{user_id}")
        if user.is_banned
        else InlineKeyboardButton(text="🚫 Забанить", callback_data=f"admin:user:ban:{user_id}")
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [ban_btn],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin:users")],
    ])
    await _safe_edit(callback, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:user:ban:"), IsAdmin())
async def admin_user_ban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as session:
        await UserRepository(session).ban(user_id)
    await callback.answer("🚫 Пользователь заблокирован", show_alert=True)
    callback.data = f"admin:user:{user_id}"
    await admin_user_detail(callback)


@router.callback_query(F.data.startswith("admin:user:unban:"), IsAdmin())
async def admin_user_unban(callback: CallbackQuery) -> None:
    user_id = int(callback.data.split(":")[-1])
    from infrastructure.database.repositories.user_repo import UserRepository
    from infrastructure.database.session import async_session_factory
    async with async_session_factory() as session:
        await UserRepository(session).unban(user_id)
    await callback.answer("✅ Пользователь разблокирован", show_alert=True)
    callback.data = f"admin:user:{user_id}"
    await admin_user_detail(callback)


# ── Каналы ───────────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:channels", IsAdmin())
async def admin_channels(callback: CallbackQuery) -> None:
    await _safe_edit(
        callback,
        " 📢 Управление каналами \n\n"
        "Каналы используются для публикации популярных треков.\n\n"
        "Для подключения канала:\n"
        "1. Добавьте бота в канал как администратора\n"
        "2. Перешлите любое сообщение из канала сюда\n\n"
        "⚙️ Функция подключения каналов в разработке.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=_BACK_BTN),
    )
    await callback.answer()


# ── Статистика ───────────────────────────────────────────────────────────────────────────────

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

    pool_stats = pool.get_stats()
    queue_stats = queue.get_stats()
    redis_ok = await cache.ping()
    cpu = psutil.cpu_percent(interval=0.1)
    ram = psutil.virtual_memory()

    popular = await cache.get_popular(limit=5)
    popular_text = "\n".join(
        f" {i+1}. {q} ({int(c)})" for i, (q, c) in enumerate(popular)
    ) or " —"

    async with async_session_factory() as session:
        total_users = (await session.execute(select(func.count()).select_from(User))).scalar_one()
        total_searches = (await session.execute(select(func.count()).select_from(Search))).scalar_one()

    await _safe_edit(
        callback,
        " 📊 Статистика \n\n"
        " Userbots: \n"
        f" 🟢 Свободных: {pool_stats['idle']}\n"
        f" 🟡 Занятых: {pool_stats['busy']}\n"
        f" 🔴 FloodWait: {pool_stats['flood']}\n"
        f" ❌ Ошибок: {pool_stats['error']}\n"
        f" ⚫ Выключено: {pool_stats['disabled']}\n\n"
        " Очередь: \n"
        f" ⏳ В очереди: {queue_stats.get('queue_size', 0)}\n"
        f" ⚙️ Обрабатывается: {queue_stats.get('processing', 0)}\n\n"
        " База данных: \n"
        f" 👥 Пользователей: {total_users}\n"
        f" 🔍 Поисков всего: {total_searches}\n\n"
        " Система: \n"
        f" 💻 CPU: {cpu}%\n"
        f" 🧠 RAM: {ram.percent}% ({ram.used // 1024 // 1024} MB)\n"
        f" 📦 Redis: {'✅' if redis_ok else '❌'}\n\n"
        " 🔥 Топ запросов: \n"
        f"{popular_text}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"),
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:stats"),
        ]]),
    )
    await callback.answer()


# ── Логи ───────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:logs", IsAdmin())
async def admin_logs(callback: CallbackQuery) -> None:
    if not os.path.exists(_LOG_PATH):
        await _safe_edit(
            callback,
            f"📋 Файл логов не найден.\n"
            f"Ожидаемый путь: <code>{_LOG_PATH}</code>\n\n"
            f"Перезапустите бот — файл создастся автоматически.",
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
        f" 📋 Последние логи ({len(lines)} строк) \n\n<pre>{last}</pre>",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🔄 Обновить", callback_data="admin:logs"),
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back"),
        ]]),
    )
    await callback.answer()


# ── Рассылка ─────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:broadcast", IsAdmin())
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_text)
    await callback.message.answer(
        "📣 Введите текст рассылки.\n"
        "Поддерживается HTML-форматирование.\n\n"
        "Для отмены отправьте /cancel"
    )
    await callback.answer()


@router.message(BroadcastStates.waiting_text, IsAdmin())
async def admin_broadcast_send(message: Message, state: FSMContext) -> None:
    await state.clear()
    text = message.text or message.caption or ""
    if not text:
        await message.answer("❌ Сообщение пустое.")
        return

    from infrastructure.database.session import async_session_factory
    from sqlalchemy import select
    from infrastructure.database.models import User

    status_msg = await message.answer("⏳ Запускаю рассылку...")

    async with async_session_factory() as session:
        res = await session.execute(
            select(User.telegram_id).where(User.is_banned == False)  # noqa: E712
        )
        user_ids = [row[0] for row in res.all()]

    sent = 0
    failed = 0
    for tg_id in user_ids:
        try:
            await message.bot.send_message(tg_id, text)
            sent += 1
        except Exception:
            failed += 1

    await status_msg.edit_text(
        f"📣 Рассылка завершена\n\n"
        f"✅ Отправлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


@router.message(Command("cancel"), IsAdmin())
async def admin_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if current:
        await state.clear()
        await message.answer("❌ Отменено.")
    else:
        await message.answer("Нечего отменять.")


# ── Назад ───────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:back", IsAdmin())
async def admin_back(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager) -> None:
    await cmd_admin(callback.message, pool, queue)  # type: ignore[arg-type]
    await callback.answer()
