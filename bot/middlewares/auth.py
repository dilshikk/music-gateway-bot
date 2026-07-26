from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from infrastructure.database.models import User
from infrastructure.database.repositories.user_repo import UserRepository
from infrastructure.database.session import async_session_factory


class AuthMiddleware(BaseMiddleware):
    """
    Регистрирует пользователя при первом обращении.
    Добавляет объект User в data["user"] для всех хендлеров.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        # Получаем telegram_user из события
        telegram_user = None
        if isinstance(event, Message):
            telegram_user = event.from_user
        elif isinstance(event, CallbackQuery):
            telegram_user = event.from_user

        if not telegram_user:
            return await handler(event, data)

        async with async_session_factory() as session:
            repo = UserRepository(session)
            user, created = await repo.get_or_create(
                telegram_id=telegram_user.id,
                username=telegram_user.username,
                first_name=telegram_user.first_name,
            )
            if created:
                import logging
                logging.getLogger(__name__).info(
                    "Новый пользователь: %d (@%s)",
                    telegram_user.id, telegram_user.username,
                )

        data["user"]    = user
        data["db_user"] = user
        return await handler(event, data)
