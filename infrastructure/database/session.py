"""
Фабрика асинхронных сессий SQLAlchemy.
Используется во всём проекте для доступа к базе данных.
"""
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config.settings import settings

# Создаём async engine один раз при старте приложения
engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_pre_ping=True,        # проверяет соединение перед использованием
    pool_size=10,
    max_overflow=20,
)

# Фабрика сессий — используется через `async with async_session_factory() as session:`
async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)
