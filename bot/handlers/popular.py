from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from core.cache_manager import CacheManager
from infrastructure.database.models import User

router = Router(name="popular")


@router.message(Command("popular"))
@router.message(F.text == "🔥 Популярное")
@router.message(F.text == "🔥 Mashhur")
@router.message(F.text == "🔥 Popular")
async def cmd_popular(
    message: Message,
    user: User,
    cache: CacheManager,
    _,
) -> None:
    popular = await cache.get_popular(limit=10)

    if not popular:
        await message.answer(_("popular-empty"))
        return

    builder = InlineKeyboardBuilder()
    for i, (query, count) in enumerate(popular, 1):
        builder.row(InlineKeyboardButton(
            text=f"{i}. {query}  ({int(count)})",
            callback_data=f"repeat:{query[:60]}",
        ))

    await message.answer(
        _("popular-title"),
        reply_markup=builder.as_markup(),
    )
