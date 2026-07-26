"""
Relay-хендлер: принимает аудио от userbot и пересылает пользователю.

Поток:
  1. Userbot отправляет аудио боту в ЛС с caption="target:{chat_id}"
  2. Бот получает сообщение — Telegram генерирует родной file_id для Bot API
  3. Бот отправляет аудио конечному пользователю по target_chat_id

Никакого скачивания на диск — всё через сервера Telegram.
"""
import logging

from aiogram import F, Router
from aiogram.types import Message

from config.settings import settings

logger = logging.getLogger(__name__)
router = Router(name="relay")

# ID userbot-аккаунтов, от которых принимаем аудио.
# Заполняется при старте из UserbotPool.
_allowed_userbot_ids: set[int] = set()


def register_userbot_ids(ids: set[int]) -> None:
    """Регистрирует ID userbot-аккаунтов для фильтрации."""
    _allowed_userbot_ids.update(ids)
    logger.info("[relay] зарегистрировано %d userbot ID: %s",
                len(_allowed_userbot_ids), _allowed_userbot_ids)


@router.message(F.audio & F.caption.startswith("target:"))
async def handle_relay_audio(message: Message) -> None:
    """
    Принимает аудио от userbot и пересылает конечному пользователю.

    Фильтры:
    - Сообщение содержит аудио
    - caption начинается с "target:"
    - Отправитель — известный userbot (или админ)
    """
    sender_id = message.from_user.id if message.from_user else 0

    # Проверяем, что отправитель — известный userbot или админ
    if sender_id not in _allowed_userbot_ids and sender_id not in settings.ADMIN_IDS:
        logger.warning(
            "[relay] отклонено аудио от неизвестного sender_id=%d", sender_id
        )
        return

    # Извлекаем target_chat_id из caption
    try:
        target_chat_id = int(message.caption.split(":", 1)[1])
    except (ValueError, IndexError):
        logger.error("[relay] невалидный caption=%r", message.caption)
        return

    audio = message.audio
    logger.info(
        "[relay] получено аудио от userbot=%d  file_id=%r  "
        "performer=%r  title=%r  target_chat_id=%d",
        sender_id, audio.file_id, audio.performer, audio.title, target_chat_id,
    )

    # Отправляем пользователю — file_id уже родной для Bot API!
    try:
        await message.bot.send_audio(
            chat_id=target_chat_id,
            audio=audio.file_id,
            performer=audio.performer,
            title=audio.title,
            duration=audio.duration,
        )
        logger.info(
            "[relay] аудио успешно отправлено  target_chat_id=%d  title=%r",
            target_chat_id, audio.title,
        )
    except Exception as e:
        logger.error(
            "[relay] ошибка отправки пользователю  target_chat_id=%d  error=%s",
            target_chat_id, e,
        )
