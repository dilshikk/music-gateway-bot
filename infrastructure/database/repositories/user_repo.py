"""
Репозиторий пользователей.
Используется в AuthMiddleware и handlers для get_or_create / increment_requests.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_telegram_id(self, telegram_id: int) -> User | None:
        result = await self.session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        telegram_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> tuple[User, bool]:
        """
        Возвращает (user, created).
        created=True — пользователь создан сейчас.
        """
        user = await self.get_by_telegram_id(telegram_id)
        if user:
            changed = False
            if username is not None and user.username != username:
                user.username = username
                changed = True
            if first_name is not None and user.first_name != first_name:
                user.first_name = first_name
                changed = True
            if changed:
                self.session.add(user)
                await self.session.commit()
                await self.session.refresh(user)
            return user, False

        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
        )
        self.session.add(user)
        await self.session.commit()
        await self.session.refresh(user)
        return user, True

    async def get_by_id(self, user_id: int) -> User | None:
        result = await self.session.execute(
            select(User).where(User.id == user_id)
        )
        return result.scalar_one_or_none()

    async def increment_requests(self, user_id: int) -> None:
        """Увеличивает счётчик запросов пользователя."""
        user = await self.get_by_id(user_id)
        if user:
            # BUG FIX: the column in models.py is 'total_requests', not 'requests_total'
            user.total_requests = (user.total_requests or 0) + 1
            user.daily_requests = (user.daily_requests or 0) + 1
            self.session.add(user)
            await self.session.commit()

    async def ban(self, user_id: int) -> None:
        user = await self.get_by_id(user_id)
        if user:
            user.is_banned = True
            self.session.add(user)
            await self.session.commit()

    async def unban(self, user_id: int) -> None:
        user = await self.get_by_id(user_id)
        if user:
            user.is_banned = False
            self.session.add(user)
            await self.session.commit()
