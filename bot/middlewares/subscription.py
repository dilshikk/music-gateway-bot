from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot
from aiogram.types import Message, TelegramObject

from bot.keyboards.subscription import build_subscription_keyboard
from infrastructure.database.repositories.base import BaseRepository
from infrastructure.database.session import async_session_factory


class SubscriptionMiddleware(BaseMiddleware):
    """
    Проверяет обязательную подписку на каналы перед выполнением запроса.
    Пропускает команды /start и /help.
    """

    SKIP_COMMANDS = {"/start", "/help"}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if not isinstance(event, Message) or not event.from_user:
            return await handler(event, data)

        # Пропускаем служебные команды
        if event.text and any(event.text.startswith(c) for c in self.SKIP_COMMANDS):
            return await handler(event, data)

        bot: Bot = data["bot"]
        not_subscribed = await self._check_subscriptions(
            bot, event.from_user.id
        )

        if not_subscribed:
            keyboard = build_subscription_keyboard(not_subscribed)
            await event.answer(
                "📢 Для использования бота подпишитесь на каналы:\n\n"
                + "\n".join(f"• {ch['title']}" for ch in not_subscribed),
                reply_markup=keyboard,
            )
            return

        return await handler(event, data)

    async def _check_subscriptions(
        self, bot: Bot, user_id: int
    ) -> list[dict]:
        """Возвращает список каналов, на которые пользователь не подписан."""
        from infrastructure.database.models import Channel
        from sqlalchemy import select

        not_subscribed = []
        async with async_session_factory() as session:
            result = await session.execute(
                select(Channel).where(Channel.enabled == True, Channel.required == True)
                .order_by(Channel.sort_order)
            )
            channels = list(result.scalars().all())

        for ch in channels:
            try:
                member = await bot.get_chat_member(ch.chat_id, user_id)
                if member.status in ("left", "kicked", "banned"):
                    not_subscribed.append({
                        "title":       ch.title,
                        "username":    ch.username,
                        "invite_link": ch.invite_link,
                        "chat_id":     ch.chat_id,
                    })
            except Exception:
                pass

        return not_subscribed
