from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.cache_manager import CacheManager
from infrastructure.database.models import User

router = Router(name="history")


@router.message(Command("history"))
@router.message(F.text == "📜 История")
async def cmd_history(message: Message, user: User, cache: CacheManager) -> None:
    history = await cache.get_history(user.id, limit=10)

    if not history:
        await message.answer("📭 История поиска пуста.")
        return

    builder = InlineKeyboardBuilder()
    for i, query in enumerate(history):
        builder.row(
            InlineKeyboardButton(
                text=f"🔍 {query}",
                callback_data=f"repeat:{query[:60]}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="🗑 Очистить историю", callback_data="clear_history")
    )

    await message.answer(
        "📜 <b>История поиска:</b>",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("repeat:"))
async def handle_repeat(callback: CallbackQuery, user: User, queue: QueueManager) -> None:  # type: ignore[name-defined]
    query = callback.data.split(":", 1)[1]
    await callback.answer(f"🔍 Ищу: {query}")
    # Делегируем в search handler через fake message
    await callback.message.answer(query)


@router.callback_query(F.data == "clear_history")
async def handle_clear_history(
    callback: CallbackQuery,
    user: User,
    cache: CacheManager,
) -> None:
    await cache.clear_history(user.id)
    await callback.message.edit_text("🗑 История очищена.")
    await callback.answer()
