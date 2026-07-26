from collections.abc import Callable

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.cache_manager import CacheManager
from infrastructure.database.models import User

router = Router(name="history")


@router.message(Command("history"))
# BUG FIX: was only "📜 История" (Russian). Added UZ and EN keyboard button texts.
@router.message(F.text.in_({"📜 История", "📜 Tarix", "📜 History"}))
async def cmd_history(
    message: Message,
    user: User,
    cache: CacheManager,
    _: Callable,
) -> None:
    history = await cache.get_history(user.id, limit=10)

    if not history:
        await message.answer(_("history-empty"))
        return

    builder = InlineKeyboardBuilder()
    for query in history:
        builder.row(
            InlineKeyboardButton(
                text=f"🔍 {query}",
                callback_data=f"repeat:{query[:60]}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text=_("history-clear"),
            callback_data="clear_history",
        )
    )

    await message.answer(
        _("history-title"),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("repeat:"))
async def handle_repeat(
    callback: CallbackQuery,
    user: User,
    _: Callable,
) -> None:
    query = callback.data.split(":", 1)[1]
    await callback.answer(f"🔍 {query}")
    await callback.message.answer(query)


@router.callback_query(F.data == "clear_history")
async def handle_clear_history(
    callback: CallbackQuery,
    user: User,
    cache: CacheManager,
    _: Callable,
) -> None:
    await cache.clear_history(user.id)
    await callback.message.edit_text(_("history-cleared"))
    await callback.answer()
