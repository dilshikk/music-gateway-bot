"""
Relay-хендлер: бот сидит в служебной группе (LOG_GROUP_ID),
слушает аудио от userbot и пересылает нужному пользователю.

Поток:
  1. Userbot получает аудио от @vkmusic_bot
  2. Userbot пересылает аудио в служебную группу с caption="user:{chat_id}"
  3. Telegram генерирует родной Bot API file_id для этого сообщения в группе
  4. Этот хендлер видит сообщение → читает chat_id из caption
  5. Отправляет аудио пользователю через bot.send_audio(file_id)

Преимущества схемы с группой:
  - Userbot не общается с ботом напрямую (не нужен bot_id в ЛС)
  - file_id генерируется ботом при получении сообщения в группе
  - Никакого скачивания на диск — всё через серверы Telegram
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
    print(f"[relay] зарегистрированы userbot ids: {ids}")


@router.message(F.audio & F.caption.startswith("user:"))
async def handle_relay_audio(message: Message) -> None:
    """
    Принимает аудио из служебной группы и пересылает пользователю.

    Фильтры:
    - Сообщение содержит аудио
    - caption начинается с "user:"
    - Сообщение пришло из служебной группы LOG_GROUP_ID
    - Отправитель — известный userbot (или админ)
    """
    chat_id = message.chat.id
    sender_id = message.from_user.id if message.from_user else 0

    print(f"[relay] входящее аудио  chat_id={chat_id}  sender_id={sender_id}  "
          f"caption={message.caption!r}")

    # Проверяем что сообщение из нашей служебной группы
    if settings.LOG_GROUP_ID != 0 and chat_id != settings.LOG_GROUP_ID:
        print(f"[relay] отклонено — не из группы LOG_GROUP_ID={settings.LOG_GROUP_ID}")
        logger.debug("[relay] отклонено: chat_id=%d != LOG_GROUP_ID=%d",
                     chat_id, settings.LOG_GROUP_ID)
        return

    # Проверяем что отправитель — известный userbot или админ
    if sender_id not in _allowed_userbot_ids and sender_id not in settings.ADMIN_IDS:
        print(f"[relay] отклонено — неизвестный sender_id={sender_id}")
        logger.warning("[relay] отклонено: неизвестный sender_id=%d", sender_id)
        return

    # Извлекаем target chat_id из caption: "user:6948392287"
    try:
        target_chat_id = int(message.caption.split(":", 1)[1])
    except (ValueError, IndexError):
        print(f"[relay] невалидный caption={message.caption!r}")
        logger.error("[relay] невалидный caption=%r", message.caption)
        return

    audio = message.audio
    print(f"[relay] пересылаем аудио  target_chat_id={target_chat_id}  "
          f"file_id={audio.file_id!r}  performer={audio.performer!r}  title={audio.title!r}")
    logger.info(
        "[relay] пересылаем  sender=%d  target=%d  file_id=%r  title=%r",
        sender_id, target_chat_id, audio.file_id, audio.title,
    )

    try:
        await message.bot.send_audio(
            chat_id=target_chat_id,
            audio=audio.file_id,  # родной Bot API file_id из группы
            performer=audio.performer,
            title=audio.title,
            duration=audio.duration,
        )
        print(f"[relay] аудио успешно отправлено пользователю {target_chat_id}")
        logger.info("[relay] успешно отправлено  target_chat_id=%d  title=%r",
                    target_chat_id, audio.title)
    except Exception as e:
        print(f"[relay] ошибка отправки  target_chat_id={target_chat_id}  error={e}")
        logger.error("[relay] ошибка отправки  target_chat_id=%d  error=%s",
                     target_chat_id, e)
