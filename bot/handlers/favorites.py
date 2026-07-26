from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from infrastructure.database.models import User
from infrastructure.database.repositories.settings_repo import FavoritesRepository
from infrastructure.database.session import async_session_factory
from infrastructure.i18n.translator import t

router = Router(name="favorites")


@router.message(Command("favorites"))
@router.message(F.text == "⭐ Избранное")
@router.message(F.text == "⭐ Sevimli")
@router.message(F.text == "⭐ Favorites")
async def cmd_favorites(message: Message, user: User, _) -> None:
    async with async_session_factory() as session:
        repo      = FavoritesRepository(session)
        favorites = await repo.get_all(user.id)

    if not favorites:
        await message.answer(_("favorites-empty"))
        return

    builder = InlineKeyboardBuilder()
    for fav in favorites[:20]:  # показываем первые 20
        track = fav.track
        label = f"🎵 {track.artist} — {track.title}"
        if len(label) > 50:
            label = label[:47] + "..."
        builder.row(
            InlineKeyboardButton(
                text=label,
                callback_data=f"fav:play:{track.id}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"fav:remove:{track.id}",
            ),
        )

    if len(favorites) > 20:
        builder.row(InlineKeyboardButton(
            text=f"... ещё {len(favorites) - 20}",
            callback_data="fav:more",
        ))

    await message.answer(
        _("favorites-title"),
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("fav:add:"))
async def fav_add(callback: CallbackQuery, user: User, _) -> None:
    track_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        repo    = FavoritesRepository(session)
        ok, reason = await repo.add(user.id, track_id)

    if ok:
        await callback.answer(_("favorites-added"), show_alert=True)
    elif reason == "full":
        await callback.answer(_("favorites-full"), show_alert=True)
    else:
        await callback.answer(_("favorites-added"))  # уже в избранном


@router.callback_query(F.data.startswith("fav:remove:"))
async def fav_remove(callback: CallbackQuery, user: User, _) -> None:
    track_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        repo = FavoritesRepository(session)
        await repo.remove(user.id, track_id)

    await callback.answer(_("favorites-removed"), show_alert=True)
    # Обновляем список
    await cmd_favorites(callback.message, user, _)  # type: ignore[arg-type]


@router.callback_query(F.data.startswith("fav:play:"))
async def fav_play(callback: CallbackQuery, user: User, _) -> None:
    """Отправляет трек из избранного повторно через file_id."""
    from sqlalchemy import select
    from infrastructure.database.models import Track

    track_id = int(callback.data.split(":")[-1])

    async with async_session_factory() as session:
        result = await session.execute(
            select(Track).where(Track.id == track_id)
        )
        track = result.scalar_one_or_none()

    if not track:
        await callback.answer("❌ Трек не найден", show_alert=True)
        return

    await callback.message.answer_audio(
        audio    = track.telegram_file_id,
        title    = track.title,
        performer = track.artist,
        duration = track.duration,
        caption  = t(user.language, "download-caption",
                     artist=track.artist or "Unknown",
                     title=track.title),
    )
    await callback.answer()
