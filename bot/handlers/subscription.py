from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.keyboards.subscription import build_subscription_keyboard
from bot.middlewares.subscription import SubscriptionMiddleware

router = Router(name="subscription")


@router.callback_query(F.data == "check_sub")
async def handle_check_subscription(callback: CallbackQuery) -> None:
    if not callback.from_user or not callback.bot:
        await callback.answer()
        return

    middleware = SubscriptionMiddleware()
    not_subscribed = await middleware._check_subscriptions(
        callback.bot, callback.from_user.id
    )

    if not_subscribed:
        keyboard = build_subscription_keyboard(not_subscribed)
        await callback.message.edit_text(
            "❌ Вы ещё не подписались на все каналы:\n\n"
            + "\n".join(f"• {ch['title']}" for ch in not_subscribed),
            reply_markup=keyboard,
        )
        await callback.answer("Подпишитесь на все каналы!", show_alert=True)
    else:
        await callback.message.edit_text(
            "✅ Отлично! Вы подписаны на все каналы.\n\n"
            "Теперь вы можете пользоваться ботом."
        )
        await callback.answer("Доступ открыт!")
