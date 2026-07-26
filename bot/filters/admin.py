from aiogram.filters import BaseFilter
from aiogram.types import Message

from config.settings import settings
from infrastructure.database.models import AdminRole
from infrastructure.database.session import async_session_factory


class IsAdmin(BaseFilter):
    """Проверяет что пользователь — администратор бота."""

    def __init__(self, min_role: AdminRole = AdminRole.MODERATOR) -> None:
        self.min_role = min_role

    async def __call__(self, message: Message) -> bool:
        if not message.from_user:
            return False

        # Суперадмины из конфига всегда имеют доступ
        if message.from_user.id in settings.ADMIN_IDS:
            return True

        from sqlalchemy import select
        from infrastructure.database.models import Admin

        async with async_session_factory() as session:
            result = await session.execute(
                select(Admin).where(Admin.telegram_id == message.from_user.id)
            )
            admin = result.scalar_one_or_none()

        if not admin:
            return False

        role_order = {
            AdminRole.MODERATOR:  1,
            AdminRole.ADMIN:      2,
            AdminRole.SUPERADMIN: 3,
        }
        return role_order[admin.role] >= role_order[self.min_role]
