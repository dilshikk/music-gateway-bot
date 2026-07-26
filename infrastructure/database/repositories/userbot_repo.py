"""
Репозиторий для работы с userbot-аккаунтами в базе данных.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import Userbot, UserbotStatus


class UserbotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[Userbot]:
        """Возвращает все userbots (не DISABLED и не ERROR)."""
        result = await self.session.execute(
            select(Userbot).where(
                Userbot.status.notin_([UserbotStatus.DISABLED])
            )
        )
        return list(result.scalars().all())

    async def get_by_id(self, userbot_id: int) -> Userbot | None:
        result = await self.session.execute(
            select(Userbot).where(Userbot.id == userbot_id)
        )
        return result.scalar_one_or_none()

    async def get_by_phone(self, phone: str) -> Userbot | None:
        result = await self.session.execute(
            select(Userbot).where(Userbot.phone == phone)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        phone: str,
        api_id: int,
        api_hash: str,
        session_string: str,
    ) -> Userbot:
        userbot = Userbot(
            phone=phone,
            api_id=api_id,
            api_hash=api_hash,
            session_string=session_string,
            status=UserbotStatus.IDLE,
        )
        self.session.add(userbot)
        await self.session.commit()
        await self.session.refresh(userbot)
        return userbot

    async def save(self, userbot: Userbot) -> None:
        """Сохраняет изменения в существующем объекте."""
        self.session.add(userbot)
        await self.session.commit()

    async def set_status(self, userbot_id: int, status: UserbotStatus) -> None:
        userbot = await self.get_by_id(userbot_id)
        if userbot:
            userbot.status = status
            await self.session.commit()

    async def delete(self, userbot_id: int) -> bool:
        userbot = await self.get_by_id(userbot_id)
        if not userbot:
            return False
        await self.session.delete(userbot)
        await self.session.commit()
        return True
