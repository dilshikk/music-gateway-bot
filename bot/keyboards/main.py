from aiogram.types import KeyboardButton, ReplyKeyboardMarkup

from infrastructure.database.models import Language


def build_main_keyboard(
    lang: Language = Language.RU,
    is_premium: bool = False,
) -> ReplyKeyboardMarkup:

    labels = {
        Language.RU: {
            "history":       "📜 История",
            "favorites":     "⭐ Избранное",
            "popular":       "🔥 Популярное",
            "settings":      "⚙️ Настройки",
            "premium":       "👑 Премиум",
            "placeholder":   "Введите название трека...",
        },
        Language.UZ: {
            "history":       "📜 Tarix",
            "favorites":     "⭐ Sevimli",
            "popular":       "🔥 Mashhur",
            "settings":      "⚙️ Sozlamalar",
            "premium":       "👑 Premium",
            "placeholder":   "Trek nomini kiriting...",
        },
        Language.EN: {
            "history":       "📜 History",
            "favorites":     "⭐ Favorites",
            "popular":       "🔥 Popular",
            "settings":      "⚙️ Settings",
            "premium":       "👑 Premium",
            "placeholder":   "Type a track name...",
        },
    }

    lb = labels.get(lang, labels[Language.RU])

    buttons = [
        [
            KeyboardButton(text=lb["history"]),
            KeyboardButton(text=lb["favorites"]),
        ],
        [
            KeyboardButton(text=lb["popular"]),
            KeyboardButton(text=lb["settings"]),
        ],
    ]

    if is_premium:
        buttons.insert(0, [KeyboardButton(text=lb["premium"])])

    return ReplyKeyboardMarkup(
        keyboard=buttons,
        resize_keyboard=True,
        input_field_placeholder=lb["placeholder"],
    )
