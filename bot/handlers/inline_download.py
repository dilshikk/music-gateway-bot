"""
Вспомогательный хэндлер: скачивает трек по inline-запросу
и обновляет кэш file_id для будущих запросов.

Проблема: в inline нельзя отправить аудио без file_id.
Решение: двухэтапный подход
  1. Первый раз — показываем кнопку "скачать в личке"
  2. После скачивания — file_id сохраняется в кэш
  3. Следующий inline-запрос — трек уже с file_id
"""
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from core.cache_manager import CacheManager
from core.search_manager import SearchManager
from infrastructure.database.models import User
from infrastructure.i18n.translator import t

logger = logging.getLogger(__name__)

router = Router(name="inline_download")


@router.callback_query(F.data.startswith("inline_dl:"))
async def inline_download(
    callback: CallbackQuery,
    user: User,
    search_manager: SearchManager,
    cache: CacheManager,
    _,
) -> None:
    """
    Скачивает трек по source_track_id и отправляет в текущий чат.
    После загрузки file_id попадает в кэш.
    """
    source_track_id = callback.data[len("inline_dl:"):]

    await callback.answer(_("download-processing"))

    # Ищем трек в поисковом кэше
    from sources.base import Track

    # Минимальный объект трека для get_audio
    track = Track(
        title          = "",
        artist         = "",
        duration       = 0,
        size           = 0,
        source_track_id = source_track_id,
    )

    try:
        audio = await search_manager.get_audio(track, user_id=user.telegram_id)
    except Exception as e:
        logger.error("[InlineDL] Error: %s", e)
        await callback.message.answer(_("download-error"))  # type: ignore[union-attr]
        return

    await callback.message.answer_audio(  # type: ignore[union-attr]
        audio     = audio.telegram_file_id,
        title     = audio.title or "",
        performer = audio.artist or "",
        duration  = audio.duration,
        caption   = t(
            user.language, "download-caption",
            artist=audio.artist or "Unknown",
            title=audio.title or "Unknown",
        ),
    )
