"""
Репозиторий настроек и избранного.
Фикс БАГ 1: selectinload для Favorite.track — нет MissingGreenlet.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from infrastructure.database.models import AudioQuality, Favorite, Language, User, UserSettings


class SettingsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, user_id: int) -> UserSettings:
        result = await self.session.execute(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        settings = result.scalar_one_or_none()
        if not settings:
            settings = UserSettings(user_id=user_id)
            self.session.add(settings)
            await self.session.commit()
            await self.session.refresh(settings)
        return settings

    async def update_language(self, user_id: int, language: Language) -> None:
        result = await self.session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user:
            user.language = language
            self.session.add(user)
        await self.session.commit()

    async def update_quality(self, user_id: int, quality: AudioQuality) -> None:
        settings = await self.get_or_create(user_id)
        settings.quality = quality
        self.session.add(settings)
        await self.session.commit()

    async def update_notifications(self, user_id: int, enabled: bool) -> None:
        settings = await self.get_or_create(user_id)
        settings.notifications = enabled
        self.session.add(settings)
        await self.session.commit()


class FavoritesRepository:
    MAX_FAVORITES = 100

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_all(self, user_id: int) -> list[Favorite]:
        """БАГ 1: selectinload — нет LazyLoad в async."""
        result = await self.session.execute(
            select(Favorite)
            .options(selectinload(Favorite.track))
            .where(Favorite.user_id == user_id)
            .order_by(Favorite.created_at.desc())
        )
        return list(result.scalars().all())

    async def add(self, user_id: int, track_id: int) -> tuple[bool, str]:
        existing = await self.get_all(user_id)
        if len(existing) >= self.MAX_FAVORITES:
            return False, "full"

        result = await self.session.execute(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.track_id == track_id,
            )
        )
        if result.scalar_one_or_none():
            return False, "duplicate"

        self.session.add(Favorite(user_id=user_id, track_id=track_id))
        await self.session.commit()
        return True, "ok"

    async def remove(self, user_id: int, track_id: int) -> bool:
        result = await self.session.execute(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.track_id == track_id,
            )
        )
        fav = result.scalar_one_or_none()
        if not fav:
            return False
        await self.session.delete(fav)
        await self.session.commit()
        return True

    async def is_favorite(self, user_id: int, track_id: int) -> bool:
        result = await self.session.execute(
            select(Favorite).where(
                Favorite.user_id == user_id,
                Favorite.track_id == track_id,
            )
        )
        return result.scalar_one_or_none() is not None
