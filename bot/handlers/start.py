from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.keyboards.main import build_main_keyboard
from infrastructure.database.models import User

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, user: User, _) -> None:
    name = message.from_user.first_name if message.from_user else ""
    premium_badge = "👑" if user.premium else ""

    await message.answer(
        _("welcome", name=name, badge=premium_badge),
        # BUG FIX: was build_main_keyboard(user.premium) — passed bool as `lang`
        # The correct signature is build_main_keyboard(lang, is_premium)
        reply_markup=build_main_keyboard(user.language, is_premium=user.premium),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, user: User, _) -> None:
    await message.answer(_("help-text"))
