"""
SQLAlchemy ORM модели.
Фиксы всех багов:
 - БАГ 5: PyEnum импортирован явно
 - БАГ 14: favorites + settings relationship в User
 - БАГ 15: back_populates вместо backref
 - БАГ 16: добавлено поле last_used в Userbot (использовалось в release_userbot)
 - БАГ 17: добавлен relationship proxy в Userbot (использовалось в _start_userbot)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Enum, Float,
    ForeignKey, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

class Base(DeclarativeBase):
    pass

# ── Enums ─────────────────────────────────────────────────────────────────────

class Language(str, PyEnum):
    RU = "ru"
    UZ = "uz"
    EN = "en"

class UserbotStatus(str, PyEnum):
    IDLE       = "idle"
    BUSY       = "busy"
    FLOOD_WAIT = "flood_wait"
    ERROR      = "error"
    DISABLED   = "disabled"

class SearchStatus(str, PyEnum):
    PENDING = "pending"
    DONE    = "done"
    FAILED  = "failed"
    CACHED  = "cached"

class AdminRole(str, PyEnum):
    SUPER_ADMIN = "super_admin"
    ADMIN       = "admin"
    MODERATOR   = "moderator"

class AudioQuality(str, PyEnum):
    ANY      = "any"
    Q128     = "128"
    Q320     = "320"
    LOSSLESS = "lossless"

# ── Tables ────────────────────────────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id:             Mapped[int]           = mapped_column(Integer, primary_key=True)
    telegram_id:    Mapped[int]           = mapped_column(BigInteger, unique=True, index=True)
    username:       Mapped[str | None]    = mapped_column(String(64))
    first_name:     Mapped[str | None]    = mapped_column(String(128))
    language:       Mapped[Language]      = mapped_column(Enum(Language), default=Language.RU)
    premium:        Mapped[bool]          = mapped_column(Boolean, default=False)
    daily_requests: Mapped[int]           = mapped_column(Integer, default=0)
    total_requests: Mapped[int]           = mapped_column(Integer, default=0)
    is_banned:      Mapped[bool]          = mapped_column(Boolean, default=False)
    ban_reason:     Mapped[str | None]    = mapped_column(Text)
    created_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at:     Mapped[datetime]      = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    favorites: Mapped[list[Favorite]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="select",
    )
    settings: Mapped[UserSettings | None] = relationship(
        back_populates="user",
        uselist=False,
        cascade="all, delete-orphan",
        lazy="select",
    )
    admin: Mapped[Admin | None] = relationship(back_populates="user", uselist=False)


class Proxy(Base):
    __tablename__ = "proxies"

    id:         Mapped[int]        = mapped_column(Integer, primary_key=True)
    # 'type' stores the scheme: "socks5", "http", etc.
    type:       Mapped[str]        = mapped_column(String(16), default="socks5")
    host:       Mapped[str]        = mapped_column(String(128))
    port:       Mapped[int]        = mapped_column(Integer)
    username:   Mapped[str | None] = mapped_column(String(64))
    password:   Mapped[str | None] = mapped_column(String(128))
    enabled:    Mapped[bool]       = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now())

    # BUG FIX 17: add back-reference so Userbot.proxy works
    userbots: Mapped[list[Userbot]] = relationship(back_populates="proxy")


class Userbot(Base):
    __tablename__ = "userbots"

    id:               Mapped[int]               = mapped_column(Integer, primary_key=True)
    phone:            Mapped[str]               = mapped_column(String(20), unique=True)
    api_id:           Mapped[int]               = mapped_column(Integer)
    api_hash:         Mapped[str]               = mapped_column(String(64))
    session_string:   Mapped[str | None]        = mapped_column(Text)
    status:           Mapped[UserbotStatus]     = mapped_column(Enum(UserbotStatus), default=UserbotStatus.IDLE)
    weight:           Mapped[int]               = mapped_column(Integer, default=1)
    daily_limit:      Mapped[int]               = mapped_column(Integer, default=200)
    requests_today:   Mapped[int]               = mapped_column(Integer, default=0)
    requests_total:   Mapped[int]               = mapped_column(Integer, default=0)
    error_count:      Mapped[int]               = mapped_column(Integer, default=0)
    flood_wait_until: Mapped[datetime | None]   = mapped_column(DateTime(timezone=True))
    # BUG FIX 16: last_used was written in release_userbot() but missing from model
    last_used:        Mapped[datetime | None]   = mapped_column(DateTime(timezone=True), nullable=True)
    proxy_id:         Mapped[int | None]        = mapped_column(ForeignKey("proxies.id"))
    created_at:       Mapped[datetime]          = mapped_column(DateTime(timezone=True), server_default=func.now())

    # BUG FIX 17: proxy relationship was missing — _start_userbot() uses model.proxy.host etc.
    proxy: Mapped[Proxy | None] = relationship(back_populates="userbots", lazy="joined")


class Source(Base):
    __tablename__ = "sources"

    id:              Mapped[int]      = mapped_column(Integer, primary_key=True)
    name:            Mapped[str]      = mapped_column(String(128), unique=True)
    bot_username:    Mapped[str]      = mapped_column(String(64), unique=True)
    type:            Mapped[str]      = mapped_column(String(32), default="telegram_bot")
    priority:        Mapped[int]      = mapped_column(Integer, default=1)
    enabled:         Mapped[bool]     = mapped_column(Boolean, default=True)
    timeout:         Mapped[int]      = mapped_column(Integer, default=30)
    success_count:   Mapped[int]      = mapped_column(Integer, default=0)
    error_count:     Mapped[int]      = mapped_column(Integer, default=0)
    avg_response_ms: Mapped[float]    = mapped_column(Float, default=0.0)
    created_at:      Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Track(Base):
    __tablename__ = "tracks"

    id:                 Mapped[int]        = mapped_column(Integer, primary_key=True)
    title:              Mapped[str]        = mapped_column(String(256))
    artist:             Mapped[str | None] = mapped_column(String(256))
    duration:           Mapped[int | None] = mapped_column(Integer)
    size:               Mapped[int | None] = mapped_column(Integer)
    telegram_file_id:   Mapped[str | None] = mapped_column(String(256), index=True)
    telegram_unique_id: Mapped[str | None] = mapped_column(String(128), unique=True, index=True)
    source_id:          Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    bitrate:            Mapped[int | None] = mapped_column(Integer)
    play_count:         Mapped[int]        = mapped_column(Integer, default=0)
    created_at:         Mapped[datetime]   = mapped_column(DateTime(timezone=True), server_default=func.now())

    favorites: Mapped[list[Favorite]] = relationship(back_populates="track")


class Search(Base):
    __tablename__ = "searches"

    id:          Mapped[int]          = mapped_column(Integer, primary_key=True)
    user_id:     Mapped[int | None]   = mapped_column(ForeignKey("users.id"), index=True)
    query:       Mapped[str]          = mapped_column(String(512))
    query_hash:  Mapped[str]          = mapped_column(String(32), index=True)
    status:      Mapped[SearchStatus] = mapped_column(Enum(SearchStatus), default=SearchStatus.PENDING)
    source_id:   Mapped[int | None]   = mapped_column(ForeignKey("sources.id"))
    duration_ms: Mapped[int | None]   = mapped_column(Integer)
    created_at:  Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class Admin(Base):
    __tablename__ = "admins"

    id:         Mapped[int]       = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]       = mapped_column(ForeignKey("users.id"), unique=True)
    role:       Mapped[AdminRole] = mapped_column(Enum(AdminRole), default=AdminRole.MODERATOR)
    created_at: Mapped[datetime]  = mapped_column(DateTime(timezone=True), server_default=func.now())

    user: Mapped[User] = relationship(back_populates="admin")


class UserSettings(Base):
    __tablename__ = "user_settings"

    id:            Mapped[int]          = mapped_column(Integer, primary_key=True)
    user_id:       Mapped[int]          = mapped_column(ForeignKey("users.id"), unique=True, index=True)
    quality:       Mapped[AudioQuality] = mapped_column(Enum(AudioQuality), default=AudioQuality.ANY)
    notifications: Mapped[bool]         = mapped_column(Boolean, default=True)
    updated_at:    Mapped[datetime]     = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    user: Mapped[User] = relationship(back_populates="settings")


class Favorite(Base):
    __tablename__ = "favorites"
    __table_args__ = (
        UniqueConstraint("user_id", "track_id", name="uq_favorite_user_track"),
    )

    id:         Mapped[int]      = mapped_column(Integer, primary_key=True)
    user_id:    Mapped[int]      = mapped_column(ForeignKey("users.id"), index=True)
    track_id:   Mapped[int]      = mapped_column(ForeignKey("tracks.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user:  Mapped[User]  = relationship(back_populates="favorites")
    track: Mapped[Track] = relationship(back_populates="favorites")
