"""
Репозиторий для работы с userbot-аккаунтами в базе данных.

BUG FIX: Старая версия принимала одну AsyncSession в конструктор.
После выхода из `async with async_session_factory() as session:` в worker.py
сессия закрывалась, и все последующие вызовы (get_all, save, delete) падали
с "Session is closed" — молча, поэтому пул стартовал пустым (0 userbots).

Решение: репозиторий принимает session_factory и открывает новую сессию
на каждую операцию. Это стандартный паттерн "Unit of Work per request".
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from infrastructure.database.models import Userbot, UserbotStatus


class UserbotRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        # BUG FIX: храним фабрику сессий, а не одну сессию.
        # Каждый метод открывает свою сессию и закрывает её после завершения.
        self._factory = session_factory

    async def get_all(self) -> list[Userbot]:
        """Возвращает все userbots (не DISABLED и не ERROR)."""
        async with self._factory() as session:
            result = await session.execute(
                select(Userbot).where(
                    Userbot.status.notin_([UserbotStatus.DISABLED])
                )
            )
            # expunge_all() отвязывает объекты от сессии,
            # чтобы они жили в памяти после её закрытия
            objects = list(result.scalars().all())
            for obj in objects:
                session.expunge(obj)
            return objects

    async def get_by_id(self, userbot_id: int) -> Userbot | None:
        async with self._factory() as session:
            result = await session.execute(
                select(Userbot).where(Userbot.id == userbot_id)
            )
            obj = result.scalar_one_or_none()
            if obj:
                session.expunge(obj)
            return obj

    async def get_by_phone(self, phone: str) -> Userbot | None:
        async with self._factory() as session:
            result = await session.execute(
                select(Userbot).where(Userbot.phone == phone)
            )
            obj = result.scalar_one_or_none()
            if obj:
                session.expunge(obj)
            return obj

    async def create(
        self,
        phone: str,
        api_id: int,
        api_hash: str,
        session_string: str,
    ) -> Userbot:
        async with self._factory() as session:
            userbot = Userbot(
                phone=phone,
                api_id=api_id,
                api_hash=api_hash,
                session_string=session_string,
                status=UserbotStatus.IDLE,
            )
            session.add(userbot)
            await session.commit()
            await session.refresh(userbot)
            session.expunge(userbot)
            return userbot

    async def save(self, userbot: Userbot) -> None:
        """
        Сохраняет изменения в существующем объекте.

        Объект может быть detached (отвязан от сессии) — merge() привязывает
        его к новой сессии и делает UPDATE только изменённых полей.
        """
        async with self._factory() as session:
            merged = await session.merge(userbot)
            await session.commit()
            await session.refresh(merged)
            # Обновляем поля оригинального объекта из merged,
            # чтобы вызывающий код видел актуальное состояние
            userbot.__dict__.update(
                {k: v for k, v in merged.__dict__.items() if not k.startswith("_")}
            )

    async def set_status(self, userbot_id: int, status: UserbotStatus) -> None:
        async with self._factory() as session:
            userbot = await session.get(Userbot, userbot_id)
            if userbot:
                userbot.status = status
                await session.commit()

    async def delete(self, userbot_id: int) -> bool:
        async with self._factory() as session:
            userbot = await session.get(Userbot, userbot_id)
            if not userbot:
                return False
            await session.delete(userbot)
            await session.commit()
            return True
