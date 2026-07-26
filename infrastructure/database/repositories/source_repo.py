"""
Репозиторий источников музыки.
"""
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import Source, User


class SourceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self) -> list[Source]:
        result = await self.session.execute(
            select(Source).order_by(Source.priority.desc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, source_id: int) -> Source | None:
        result = await self.session.execute(
            select(Source).where(Source.id == source_id)
        )
        return result.scalar_one_or_none()

    async def set_enabled(self, source_id: int, enabled: bool) -> None:
        source = await self.get_by_id(source_id)
        if source:
            source.enabled = enabled
            self.session.add(source)
            await self.session.commit()

    async def get_or_create_vk(self) -> Source:
        """Ensure the default VK Music Bot source exists in DB."""
        result = await self.session.execute(
            select(Source).where(Source.bot_username == "vkmusic_bot")
        )
        source = result.scalar_one_or_none()
        if not source:
            source = Source(
                name="VK Music Bot",
                bot_username="vkmusic_bot",
                type="telegram_bot",
                priority=10,
                enabled=True,
                timeout=30,
            )
            self.session.add(source)
            await self.session.commit()
            await self.session.refresh(source)
        return source

    async def count_all_users(self) -> int:
        result = await self.session.execute(
            select(func.count()).select_from(User)
        )
        return result.scalar_one()
