from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ─── Data Transfer Objects ────────────────────────────────────────────────────

@dataclass
class Track:
    """Единый формат трека для всех источников."""
    title: str
    duration: int          # секунды
    size: int              # байты
    source_track_id: str   # внутренний ID в источнике (callback_data и т.п.)
    artist: str = ""
    bitrate: int = 0       # kbps
    is_lossless: bool = False
    thumbnail_url: str = ""
    raw: dict = field(default_factory=dict)  # оригинальный ответ источника
    # raw намеренно оставлен как dict без схемы — ключи search_chat_id,
    # search_msg_id, parsed_at должны проходить сквозь asdict/Track(**t) без обрезки.

@dataclass
class AudioFile:
    """Полученный аудиофайл, готовый к отправке."""
    telegram_file_id: str
    telegram_unique_id: str
    title: str
    artist: str
    duration: int
    size: int
    file_path: str | None = None   # если скачан локально
    already_sent: bool = False     # True когда userbot переслал в LOG_GROUP_ID напрямую

@dataclass
class SearchResult:
    """Результат поиска от источника."""
    tracks: list[Track]
    total: int
    page: int = 1
    has_next: bool = False
    source_name: str = ""
    query: str = ""
    # BUG FIX: datetime.utcnow() deprecated since Python 3.12 — use timezone-aware datetime
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

# ─── Exceptions ───────────────────────────────────────────────────────────────

class SourceError(Exception):
    """Базовая ошибка источника."""

class SourceUnavailableError(SourceError):
    """Источник недоступен."""

class SourceTimeoutError(SourceError):
    """Источник не ответил в отведённое время."""

class TrackNotFoundError(SourceError):
    """Трек не найден."""

class SourceFloodWaitError(SourceError):
    """Источник вернул FloodWait."""
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds
        super().__init__(f"FloodWait: {seconds}s")

# ─── Abstract Base ────────────────────────────────────────────────────────────

class MusicSource(ABC):
    """
    Абстрактный класс источника музыки.

    Чтобы добавить новый источник — создай файл sources/my_source.py,
    унаследуй MusicSource и реализуй все абстрактные методы.
    Больше ничего менять не нужно.
    """

    # Обязательные атрибуты класса
    name: str          # "VK Music Bot"
    bot_username: str  # "vkmusic_bot"
    source_type: str   # "telegram_bot" | "api" | "database"

    def __init__(
        self,
        priority: int = 1,
        timeout: int = 30,
        enabled: bool = True,
    ) -> None:
        self.priority = priority
        self.timeout = timeout
        self.enabled = enabled
        self._success_count = 0
        self._error_count = 0
        self._total_response_ms = 0.0

    # ── Обязательные методы ───────────────────────────────────────────────────

    @abstractmethod
    async def search(self, query: str, page: int = 1) -> SearchResult:
        """Поиск треков по запросу."""
        ...

    @abstractmethod
    async def get_audio(self, track: Track) -> AudioFile:
        """Получить аудиофайл по треку."""
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Проверка доступности источника."""
        ...

    # ── Опциональные методы ───────────────────────────────────────────────────

    async def get_page(self, query: str, page: int) -> SearchResult:
        """Получить конкретную страницу результатов. По умолчанию = search()."""
        return await self.search(query, page=page)

    # ── Статистика (вызывается из Search Manager) ─────────────────────────────

    def record_success(self, response_ms: float) -> None:
        self._success_count += 1
        self._total_response_ms += response_ms

    def record_error(self) -> None:
        self._error_count += 1

    @property
    def avg_response_ms(self) -> float:
        if self._success_count == 0:
            return 0.0
        return self._total_response_ms / self._success_count

    @property
    def error_rate(self) -> float:
        total = self._success_count + self._error_count
        if total == 0:
            return 0.0
        return self._error_count / total

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} "
            f"name={self.name!r} "
            f"priority={self.priority} "
            f"enabled={self.enabled}>"
        )
