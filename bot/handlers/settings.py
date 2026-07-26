"""
Хэндлер настроек.
Фикс БАГ 9: не трогаем detached user — работаем с new_lang напрямую.
"""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from infrastructure.database.models import AudioQuality, Language, User
from infrastructure.database.repositories.settings_repo import SettingsRepository
from infrastructure.database.session import async_session_factory
from infrastructure.i18n.translator import t

router = Router(name="settings")

LANGUAGE_LABELS = {
    Language.RU: "🇷🇺 Русский",
    Language.UZ: "🇺🇿 O'zbek",
    Language.EN: "🇬🇧 English",
}

QUALITY_KEYS = {
    AudioQuality.ANY:      "quality-any",
    AudioQuality.Q128:     "quality-128",
    AudioQuality.Q320:     "quality-320",
    AudioQuality.LOSSLESS: "quality-lossless",
}


@router.message(Command("settings"))
@router.message(F.text.in_({"⚙️ Настройки", "⚙️ Sozlamalar", "⚙️ Settings"}))
async def cmd_settings(message: Message, user: User, _) -> None:
    async with async_session_factory() as session:
        repo     = SettingsRepository(session)
        settings = await repo.get_or_create(user.id)

    quality_label = t(user.language, QUALITY_KEYS[settings.quality])
    notif_label   = "✅" if settings.notifications else "❌"

    await message.answer(
        _(
            "settings-title",
            language=LANGUAGE_LABELS[user.language],
            quality=quality_label,
            notifications=notif_label,
        ),
        reply_markup=_build_settings_keyboard(user.language),
    )


def _build_settings_keyboard(lang: Language) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t(lang, "settings-language"),      callback_data="settings:language")],
        [InlineKeyboardButton(text=t(lang, "settings-quality"),       callback_data="settings:quality")],
        [InlineKeyboardButton(text=t(lang, "settings-notifications"), callback_data="settings:notifications")],
        [InlineKeyboardButton(text=t(lang, "btn-close"),              callback_data="close")],
    ])


@router.callback_query(F.data == "settings:language")
async def settings_language(callback: CallbackQuery, user: User, _) -> None:
    builder = InlineKeyboardBuilder()
    for lang, label in LANGUAGE_LABELS.items():
        mark = "✓ " if lang == user.language else ""
        builder.row(InlineKeyboardButton(
            text=f"{mark}{label}",
            callback_data=f"settings:set_lang:{lang.value}",
        ))
    builder.row(InlineKeyboardButton(text=_("btn-back"), callback_data="settings:back"))
    await callback.message.edit_text(
        "🌐 <b>Выберите язык / Tilni tanlang / Choose language:</b>",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:set_lang:"))
async def settings_set_language(callback: CallbackQuery, user: User) -> None:
    lang_str = callback.data.split(":")[-1]
    try:
        new_lang = Language(lang_str)
    except ValueError:
        await callback.answer("❌ Unknown language")
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        await repo.update_language(user.id, new_lang)

    # БАГ 9: используем new_lang напрямую, не трогаем detached user
    await callback.answer(t(new_lang, "settings-saved"), show_alert=True)
    await callback.message.delete()


@router.callback_query(F.data == "settings:quality")
async def settings_quality(callback: CallbackQuery, user: User, _) -> None:
    async with async_session_factory() as session:
        repo          = SettingsRepository(session)
        user_settings = await repo.get_or_create(user.id)

    builder = InlineKeyboardBuilder()
    for quality, key in QUALITY_KEYS.items():
        mark = "✓ " if quality == user_settings.quality else ""
        builder.row(InlineKeyboardButton(
            text=f"{mark}{t(user.language, key)}",
            callback_data=f"settings:set_quality:{quality.value}",
        ))
    builder.row(InlineKeyboardButton(text=_("btn-back"), callback_data="settings:back"))
    await callback.message.edit_text(
        f"🎵 <b>{t(user.language, 'settings-quality')}:</b>",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("settings:set_quality:"))
async def settings_set_quality(callback: CallbackQuery, user: User, _) -> None:
    quality_str = callback.data.split(":")[-1]
    try:
        new_quality = AudioQuality(quality_str)
    except ValueError:
        await callback.answer("❌ Unknown quality")
        return

    async with async_session_factory() as session:
        repo = SettingsRepository(session)
        await repo.update_quality(user.id, new_quality)

    await callback.answer(_("settings-saved"), show_alert=True)
    await settings_quality(callback, user, _)


@router.callback_query(F.data == "settings:notifications")
async def settings_notifications(callback: CallbackQuery, user: User, _) -> None:
    async with async_session_factory() as session:
        repo          = SettingsRepository(session)
        user_settings = await repo.get_or_create(user.id)
        new_state     = not user_settings.notifications
        await repo.update_notifications(user.id, new_state)

    status = "✅" if new_state else "❌"
    await callback.answer(
        f"{t(user.language, 'settings-notifications')}: {status}",
        show_alert=True,
    )


@router.callback_query(F.data == "settings:back")
async def settings_back(callback: CallbackQuery, user: User, _) -> None:
    async with async_session_factory() as session:
        repo          = SettingsRepository(session)
        user_settings = await repo.get_or_create(user.id)

    quality_label = t(user.language, QUALITY_KEYS[user_settings.quality])
    notif_label   = "✅" if user_settings.notifications else "❌"

    await callback.message.edit_text(
        _(
            "settings-title",
            language=LANGUAGE_LABELS[user.language],
            quality=quality_label,
            notifications=notif_label,
        ),
        reply_markup=_build_settings_keyboard(user.language),
    )
    await callback.answer()
