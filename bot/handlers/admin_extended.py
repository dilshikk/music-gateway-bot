"""
Расширенные команды администратора:
  /logs      — последние ошибки из Redis
  /broadcast — массовая рассылка
  /ban       — блокировка пользователя
  /unban     — разблокировка
  /addadmin  — добавить администратора
"""
import logging
from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.filters.admin import IsAdmin
from infrastructure.database.models import Admin, AdminRole, User
from infrastructure.database.repositories.user_repo import UserRepository
from infrastructure.database.session import async_session_factory

logger = logging.getLogger(__name__)
router = Router(name="admin_extended")


class BroadcastStates(StatesGroup):
    waiting_text    = State()
    waiting_confirm = State()


# ── /logs ─────────────────────────────────────────────────────────────────────

@router.message(Command("logs"), IsAdmin())
async def cmd_logs(message: Message) -> None:
    from core.cache_manager import CacheManager
    from redis.asyncio import Redis
    from config.settings import settings

    redis = Redis.from_url(settings.redis_url, decode_responses=True)

    try:
        # Последние 10 ошибок из списка (пишутся через log_error)
        errors = await redis.lrange("logs:errors", 0, 9)
        searches = await redis.lrange("logs:searches", 0, 4)
        flood    = await redis.lrange("logs:flood_wait", 0, 4)
    finally:
        await redis.aclose()

    def fmt(items: list, title: str) -> str:
        if not items:
            return f"<b>{title}:</b>\n  —\n"
        lines = "\n".join(f"  • {i}" for i in items)
        return f"<b>{title}:</b>\n{lines}\n"

    await message.answer(
        "📋 <b>Последние логи</b>\n\n"
        + fmt(errors,   "🔴 Ошибки")
        + fmt(searches, "🔍 Поиски")
        + fmt(flood,    "🚫 FloodWait"),
    )


# ── Логгер ошибок (пишем в Redis) ─────────────────────────────────────────────

async def log_error(redis, message: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {message[:120]}"
    await redis.lpush("logs:errors", line)
    await redis.ltrim("logs:errors", 0, 99)  # храним последние 100


async def log_search(redis, user_id: int, query: str, status: str) -> None:
    ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] user={user_id} q={query!r} status={status}"
    await redis.lpush("logs:searches", line)
    await redis.ltrim("logs:searches", 0, 199)


async def log_flood_wait(redis, userbot_id: int, seconds: int) -> None:
    ts   = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] userbot={userbot_id} wait={seconds}s"
    await redis.lpush("logs:flood_wait", line)
    await redis.ltrim("logs:flood_wait", 0, 49)


# ── /ban ──────────────────────────────────────────────────────────────────────

@router.message(Command("ban"), IsAdmin(min_role=AdminRole.ADMIN))
async def cmd_ban(message: Message) -> None:
    parts = message.text.split(maxsplit=2) if message.text else []
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban <telegram_id> [причина]")
        return

    target_id = int(parts[1])
    reason    = parts[2] if len(parts) > 2 else "Нарушение правил"

    async with async_session_factory() as session:
        repo = UserRepository(session)
        user = await repo.get_by_telegram_id(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден.")
            return
        user.is_banned  = True
        user.ban_reason = reason
        await repo.save(user)

    await message.answer(
        f"✅ Пользователь <code>{target_id}</code> заблокирован.\n"
        f"Причина: {reason}"
    )
    logger.info("Ban: %d причина=%s", target_id, reason)


@router.message(Command("unban"), IsAdmin(min_role=AdminRole.ADMIN))
async def cmd_unban(message: Message) -> None:
    parts = message.text.split() if message.text else []
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban <telegram_id>")
        return

    target_id = int(parts[1])
    async with async_session_factory() as session:
        repo = UserRepository(session)
        user = await repo.get_by_telegram_id(target_id)
        if not user:
            await message.answer("❌ Пользователь не найден.")
            return
        user.is_banned  = False
        user.ban_reason = None
        await repo.save(user)

    await message.answer(f"✅ Пользователь <code>{target_id}</code> разблокирован.")


# ── /addadmin ─────────────────────────────────────────────────────────────────

@router.message(Command("addadmin"), IsAdmin(min_role=AdminRole.SUPERADMIN))
async def cmd_add_admin(message: Message) -> None:
    """Использование: /addadmin <telegram_id> <moderator|admin|superadmin>"""
    parts = message.text.split() if message.text else []
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer(
            "Использование: /addadmin <telegram_id> <moderator|admin|superadmin>"
        )
        return

    target_id   = int(parts[1])
    role_str    = parts[2].lower()
    role_map    = {
        "moderator":  AdminRole.MODERATOR,
        "admin":      AdminRole.ADMIN,
        "superadmin": AdminRole.SUPERADMIN,
    }
    role = role_map.get(role_str)
    if not role:
        await message.answer("❌ Неверная роль. Доступно: moderator, admin, superadmin")
        return

    async with async_session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(Admin).where(Admin.telegram_id == target_id)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.role = role
            session.add(existing)
        else:
            session.add(Admin(telegram_id=target_id, role=role))

        await session.commit()

    await message.answer(
        f"✅ Пользователь <code>{target_id}</code> → роль <b>{role.value}</b>"
    )


# ── /broadcast ────────────────────────────────────────────────────────────────

@router.message(Command("broadcast"), IsAdmin(min_role=AdminRole.ADMIN))
async def cmd_broadcast_start(message: Message, state: FSMContext) -> None:
    await state.set_state(BroadcastStates.waiting_text)
    await message.answer(
        "📣 Введите текст рассылки.\n\n"
        "Поддерживается HTML-форматирование.\n"
        "/cancel — отмена"
    )


@router.message(BroadcastStates.waiting_text, IsAdmin())
async def broadcast_preview(message: Message, state: FSMContext) -> None:
    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.")
        return

    await state.update_data(broadcast_text=message.text)
    await state.set_state(BroadcastStates.waiting_confirm)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Отправить", callback_data="broadcast:confirm"),
        InlineKeyboardButton(text="❌ Отмена",    callback_data="broadcast:cancel"),
    ]])

    await message.answer(
        "<b>Предпросмотр рассылки:</b>\n\n"
        f"{message.text}\n\n"
        "Подтвердите отправку:",
        reply_markup=keyboard,
    )


@router.callback_query(F.data == "broadcast:confirm", IsAdmin())
async def broadcast_send(callback: CallbackQuery, state: FSMContext) -> None:  # type: ignore[name-defined]
    data = await state.get_data()
    await state.clear()
    text = data.get("broadcast_text", "")

    await callback.message.edit_text("📤 Начинаю рассылку...")
    await callback.answer()

    sent = 0
    failed = 0

    async with async_session_factory() as session:
        from sqlalchemy import select
        result = await session.execute(
            select(User).where(User.is_banned == False)
        )
        users = list(result.scalars().all())

    for user in users:
        try:
            await callback.bot.send_message(user.telegram_id, text)
            sent += 1
        except Exception:
            failed += 1
        # Небольшая пауза чтобы не словить FloodWait от Bot API
        if sent % 25 == 0:
            import asyncio
            await asyncio.sleep(1)

    await callback.message.answer(
        f"✅ Рассылка завершена.\n\n"
        f"📨 Отправлено: <b>{sent}</b>\n"
        f"❌ Ошибок: <b>{failed}</b>"
    )


@router.callback_query(F.data == "broadcast:cancel", IsAdmin())
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:  # type: ignore[name-defined]
    await state.clear()
    await callback.message.edit_text("❌ Рассылка отменена.")
    await callback.answer()
