from collections.abc import Callable

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.keyboards.subscription import build_subscription_keyboard
from bot.middlewares.subscription import SubscriptionMiddleware

router = Router(name="subscription")


@router.callback_query(F.data == "check_sub")
async def handle_check_subscription(
    callback: CallbackQuery,
    _: Callable,
) -> None:
    if not callback.from_user or not callback.bot:
        await callback.answer()
        return

    middleware = SubscriptionMiddleware()
    not_subscribed = await middleware._check_subscriptions(
        callback.bot, callback.from_user.id
    )

    if not_subscribed:
        channels_text = "\n".join(f"• {ch['title']}" for ch in not_subscribed)
        keyboard = build_subscription_keyboard(not_subscribed)
        await callback.message.edit_text(
            _("subscription-fail", channels=channels_text),
            reply_markup=keyboard,
        )
        await callback.answer(_("subscription-check"), show_alert=True)
    else:
        await callback.message.edit_text(_("subscription-success"))
        await callback.answer(_("subscription-check"))
