"""
Репозиторий для работы с userbot-аккаунтами в базе данных.

Поддерживает два паттерна:
1. Передача готовой сессии:  UserbotRepository(session)
2. Передача фабрики сессий:  UserbotRepository(session_factory=factory)
   — каждый метод открывает и закрывает сессию самостоятельно.
"""
from __future__ import annotations

from typing import Any, Callable, Coroutine

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from infrastructure.database.models import Userbot, UserbotStatus


class UserbotRepository:
    def __init__(
        self,
        session: AsyncSession | None = None,
        *,
        session_factory: Callable[..., Any] | None = None,
    ) -> None:
        if session is None and session_factory is None:
            raise ValueError("Provide either 'session' or 'session_factory'")
        self._session = session
        self._factory = session_factory

    # ── Внутренний хелпер ────────────────────────────────────────────────────

    async def _get_session(self) -> tuple[AsyncSession, bool]:
        """
        Возвращает (session, should_close).
        Если используется фабрика — создаёт новую сессию (нужно закрыть).
        Если передана готовая сессия — возвращает её (не закрывать).
        """
        if self._session is not None:
            return self._session, False
        assert self._factory is not None
        session = self._factory()
        await session.__aenter__()
        return session, True

    # ── CRUD ─────────────────────────────────────────────────────────────────

    async def get_all(self) -> list[Userbot]:
        """Возвращает все userbots (кроме DISABLED)."""
        session, close = await self._get_session()
        try:
            result = await session.execute(
                select(Userbot).where(
                    Userbot.status.notin_([UserbotStatus.DISABLED])
                )
            )
            rows = list(result.scalars().all())
            # expunge чтобы объекты жили после закрытия сессии
            if close:
                for row in rows:
                    session.expunge(row)
            return rows
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def get_by_id(self, userbot_id: int) -> Userbot | None:
        session, close = await self._get_session()
        try:
            result = await session.execute(
                select(Userbot).where(Userbot.id == userbot_id)
            )
            row = result.scalar_one_or_none()
            if close and row is not None:
                session.expunge(row)
            return row
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def get_by_phone(self, phone: str) -> Userbot | None:
        session, close = await self._get_session()
        try:
            result = await session.execute(
                select(Userbot).where(Userbot.phone == phone)
            )
            row = result.scalar_one_or_none()
            if close and row is not None:
                session.expunge(row)
            return row
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def create(
        self,
        phone: str,
        api_id: int,
        api_hash: str,
        session_string: str,
    ) -> Userbot:
        session, close = await self._get_session()
        try:
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
            if close:
                session.expunge(userbot)
            return userbot
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def save(self, userbot: Userbot) -> None:
        """Сохраняет изменения в существующем объекте."""
        session, close = await self._get_session()
        try:
            merged = await session.merge(userbot)
            await session.commit()
            # Синхронизируем изменения обратно в оригинальный объект
            if close:
                userbot.__dict__.update(
                    {k: v for k, v in merged.__dict__.items() if not k.startswith("_")}
                )
                session.expunge(merged)
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def set_status(self, userbot_id: int, status: UserbotStatus) -> None:
        session, close = await self._get_session()
        try:
            result = await session.execute(
                select(Userbot).where(Userbot.id == userbot_id)
            )
            userbot = result.scalar_one_or_none()
            if userbot:
                userbot.status = status
                await session.commit()
        finally:
            if close:
                await session.__aexit__(None, None, None)

    async def delete(self, userbot_id: int) -> bool:
        session, close = await self._get_session()
        try:
            result = await session.execute(
                select(Userbot).where(Userbot.id == userbot_id)
            )
            userbot = result.scalar_one_or_none()
            if not userbot:
                return False
            await session.delete(userbot)
            await session.commit()
            return True
        finally:
            if close:
                await session.__aexit__(None, None, None)
