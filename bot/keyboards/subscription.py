from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def build_subscription_keyboard(channels: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for ch in channels:
        url = ch.get("invite_link") or f"https://t.me/{ch['username'].lstrip('@')}"
        builder.row(
            InlineKeyboardButton(text=f"📢 {ch['title']}", url=url)
        )
    builder.row(
        InlineKeyboardButton(text="✅ Проверить подписку", callback_data="check_sub")
    )
    return builder.as_markup()
