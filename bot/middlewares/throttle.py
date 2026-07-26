import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

# telegram_id → timestamp последнего сообщения
_last_message: dict[int, float] = {}
THROTTLE_SECONDS = 0.5


class ThrottleMiddleware(BaseMiddleware):
    """Антиспам: не более 1 сообщения каждые 0.5 сек с одного аккаунта."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        uid  = event.from_user.id
        now  = time.monotonic()
        last = _last_message.get(uid, 0)

        if now - last < THROTTLE_SECONDS:
            return  # молча игнорируем

        _last_message[uid] = now
        return await handler(event, data)
