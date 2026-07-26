"""
Базовый репозиторий — общий шаблон для всех репозиториев.
"""
from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import Base

ModelT = TypeVar("ModelT", bound=Base)


class BaseRepository(Generic[ModelT]):
    """Базовый CRUD-репозиторий для SQLAlchemy AsyncSession."""

    model_class: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, pk: int) -> ModelT | None:
        result = await self.session.execute(
            select(self.model_class).where(self.model_class.id == pk)  # type: ignore[attr-defined]
        )
        return result.scalar_one_or_none()

    async def get_all(self) -> list[ModelT]:
        result = await self.session.execute(select(self.model_class))
        return list(result.scalars().all())

    async def add(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj

    async def delete(self, obj: ModelT) -> None:
        await self.session.delete(obj)
        await self.session.commit()

    async def save(self, obj: ModelT) -> ModelT:
        self.session.add(obj)
        await self.session.commit()
        await self.session.refresh(obj)
        return obj
