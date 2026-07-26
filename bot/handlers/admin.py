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

# ── Управление Userbot ────────────────────────────────────────────────────────

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


# BUG FIX: previous filter F.data.startswith("admin:ub:") & ~F.data.contains("add")
# was intercepting ALL sub-actions (enable/disable/restart/delete) before their
# specific handlers could fire. Now we match only exact "admin:ub:{int}" (3 parts).
@router.callback_query(F.data.func(_is_ub_detail), IsAdmin())
async def admin_userbot_detail(
    callback: CallbackQuery,
    pool: UserbotPool,
) -> None:
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
    # Вернуть список userbots
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


# ── Добавление Userbot через FSM ──────────────────────────────────────────────

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
    await message.answer(
        "📋 Введите session_string:\n\n"
        "Получить можно через команду на сервере:\n"
        "<code>python3 -c \"\n"
        "from pyrogram import Client\n"
        "import asyncio\n"
        "async def gen():\n"
        "    async with Client('s', api_id=API_ID, api_hash='API_HASH') as c:\n"
        "        print(await c.export_session_string())\n"
        "asyncio.run(gen())\n"
        "\"</code>"
    )


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
        f" {i+1}. {q} ({int(c)})" for i, (q, c) in enumerate(popular)
    ) or " —"

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


@router.callback_query(F.data == "admin:back", IsAdmin())
async def admin_back(callback: CallbackQuery, pool: UserbotPool, queue: QueueManager) -> None:
    await cmd_admin(callback.message, pool, queue)  # type: ignore[arg-type]
    await callback.answer()
