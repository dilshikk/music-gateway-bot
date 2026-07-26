"""
Middleware для i18n.
Добавляет функцию _() в data хэндлеров — готовый переводчик
привязанный к языку текущего пользователя.
"""
from collections.abc import Callable
from typing import Any, Awaitable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from infrastructure.database.models import Language, User
from infrastructure.i18n.translator import t


class I18nMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("user")
        lang = user.language if user else Language.RU

        # Удобная обёртка — вызываем как _("key", name="Ivan")
        def _(key: str, **kwargs) -> str:
            return t(lang, key, **kwargs)

        data["_"]    = _
        data["lang"] = lang
        return await handler(event, data)
