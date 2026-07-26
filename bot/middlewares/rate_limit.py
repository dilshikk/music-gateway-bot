from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import Message, TelegramObject

from config.settings import settings
from core.cache_manager import CacheManager
from infrastructure.database.models import User


class RateLimitMiddleware(BaseMiddleware):
    """
    Блокирует запросы при превышении лимита.
    Premium пользователи получают увеличенный лимит.
    Администраторы (ADMIN_IDS) полностью освобождены от ограничений.
    """

    def __init__(self, cache: CacheManager) -> None:
        self._cache = cache

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message):
            return await handler(event, data)

        user: User | None = data.get("user")
        if not user or user.is_banned:
            await event.answer("🚫 Вы заблокированы.")
            return

        # Admins bypass rate limiting completely
        if event.from_user and event.from_user.id in settings.ADMIN_IDS:
            return await handler(event, data)

        allowed, retry_after = await self._cache.check_rate_limit(user.id)
        if not allowed:
            m, s = divmod(retry_after, 60)
            if m:
                time_str = f"{m} мин {s} сек"
            else:
                time_str = f"{s} сек"
            await event.answer(
                f"⏳ Слишком много запросов.\n"
                f"Подождите {time_str}.",
            )
            return

        return await handler(event, data)
