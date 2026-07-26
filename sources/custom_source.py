"""
CustomMusicSource — полностью рабочий пример источника через HTTP API.

Реализует интеграцию с произвольным REST API, которое отдаёт треки.
Используй как шаблон для подключения любого внешнего музыкального сервиса:
  - Собственный API
  - Публичный музыкальный сервис
  - Любой JSON-эндпоинт

Чтобы подключить свой источник:
  1. Скопируй этот файл: cp sources/custom_source.py sources/my_service.py
  2. Замени BASE_URL, API_KEY_ENV, логику парсинга
  3. Зарегистрируй в core/worker.py: registry.register(MyService(...))
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import aiohttp

from sources.base import (
    AudioFile,
    MusicSource,
    SearchResult,
    SourceFloodWaitError,
    SourceTimeoutError,
    SourceUnavailableError,
    Track,
    TrackNotFoundError,
)

logger = logging.getLogger(__name__)


# ─── Конфигурация ─────────────────────────────────────────────────────────────

# Измени на URL своего API
BASE_URL: str = os.getenv("CUSTOM_SOURCE_BASE_URL", "https://api.example-music.com/v1")

# Опциональный API-ключ (из переменной окружения)
API_KEY_ENV: str = "CUSTOM_SOURCE_API_KEY"


# ─── DTO для внутреннего парсинга ─────────────────────────────────────────────

@dataclass
class _RawTrack:
    """Промежуточный объект при парсинге ответа API."""
    id: str
    title: str
    artist: str
    duration: int        # секунды
    size: int            # байты
    bitrate: int         # kbps
    is_lossless: bool
    audio_url: str       # прямая ссылка на MP3/FLAC
    thumbnail_url: str


# ─── Источник ─────────────────────────────────────────────────────────────────

class CustomMusicSource(MusicSource):
    """
    Источник музыки через произвольный HTTP/REST API.

    Поддерживает:
      - Постраничный поиск
      - Прямое скачивание аудиофайла
      - Health-check через /ping или /status эндпоинт
      - Опциональную Bearer-авторизацию
      - Автоматические retry с exponential backoff
      - Таймаут на каждый HTTP-запрос

    Пример API-ответа на /search:
    {
        "total": 123,
        "page": 1,
        "has_next": true,
        "tracks": [
            {
                "id": "abc123",
                "title": "Song Name",
                "artist": "Artist Name",
                "duration": 210,
                "size": 5242880,
                "bitrate": 320,
                "is_lossless": false,
                "audio_url": "https://cdn.example.com/abc123.mp3",
                "thumbnail_url": "https://cdn.example.com/abc123.jpg"
            }
        ]
    }
    """

    name         = "Custom Music API"
    bot_username = ""                  # не используется для HTTP-источника
    source_type  = "api"

    # Сколько результатов запрашивать за раз
    PAGE_SIZE = 10

    # Retry-параметры
    MAX_RETRIES    = 3
    RETRY_BACKOFF  = 1.5   # множитель задержки

    def __init__(
        self,
        base_url: str = BASE_URL,
        api_key: str | None = None,
        priority: int = 5,
        timeout: int = 15,
        enabled: bool = True,
    ) -> None:
        super().__init__(priority=priority, timeout=timeout, enabled=enabled)
        self._base_url = base_url.rstrip("/")
        self._api_key  = api_key or os.getenv(API_KEY_ENV, "")
        self._session: aiohttp.ClientSession | None = None

    # ── HTTP-сессия ───────────────────────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        """Возвращает переиспользуемую aiohttp-сессию (создаёт если нет)."""
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {
                "Accept": "application/json",
                "User-Agent": "MusicGatewayBot/1.0",
            }
            if self._api_key:
                headers["Authorization"] = f"Bearer {self._api_key}"

            timeout = aiohttp.ClientTimeout(total=self.timeout)
            self._session = aiohttp.ClientSession(
                base_url=self._base_url,
                headers=headers,
                timeout=timeout,
            )
        return self._session

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    # ── Поиск ─────────────────────────────────────────────────────────────────

    async def search(self, query: str, page: int = 1) -> SearchResult:
        """Поиск треков с retry-логикой и записью метрик."""
        start = time.monotonic()
        try:
            result = await self._search_with_retry(query, page)
            self.record_success((time.monotonic() - start) * 1000)
            return result
        except (SourceTimeoutError, SourceUnavailableError, SourceFloodWaitError):
            self.record_error()
            raise
        except Exception as e:
            self.record_error()
            raise SourceUnavailableError(f"Непредвиденная ошибка поиска: {e}") from e

    async def _search_with_retry(self, query: str, page: int) -> SearchResult:
        """Выполняет поиск с exponential backoff retry."""
        last_error: Exception | None = None

        for attempt in range(self.MAX_RETRIES):
            try:
                return await self._search_request(query, page)

            except aiohttp.ClientResponseError as e:
                if e.status == 429:
                    # Rate limit: читаем Retry-After если есть
                    retry_after = int(e.headers.get("Retry-After", 5))  # type: ignore[union-attr]
                    raise SourceFloodWaitError(retry_after) from e
                if e.status >= 500:
                    # Серверная ошибка — повторим
                    last_error = SourceUnavailableError(
                        f"Сервер ответил {e.status}"
                    )
                    delay = self.RETRY_BACKOFF ** attempt
                    logger.warning(
                        "Сервер %s вернул %d, попытка %d/%d, ждём %.1fs",
                        self.name, e.status, attempt + 1, self.MAX_RETRIES, delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise SourceUnavailableError(str(e)) from e

            except asyncio.TimeoutError as e:
                last_error = SourceTimeoutError(
                    f"Таймаут запроса к {self.name} (попытка {attempt + 1})"
                )
                logger.warning("Таймаут %s, попытка %d/%d", self.name, attempt + 1, self.MAX_RETRIES)
                await asyncio.sleep(self.RETRY_BACKOFF ** attempt)
                continue

            except aiohttp.ClientConnectionError as e:
                last_error = SourceUnavailableError(f"Ошибка соединения: {e}")
                await asyncio.sleep(self.RETRY_BACKOFF ** attempt)
                continue

        raise last_error or SourceUnavailableError(f"Превышено число попыток для {self.name}")

    async def _search_request(self, query: str, page: int) -> SearchResult:
        """Один HTTP-запрос на поиск. Парсит ответ и возвращает SearchResult."""
        session = await self._get_session()
        params = {
            "q":        query,
            "page":     page,
            "per_page": self.PAGE_SIZE,
        }

        async with session.get("/search", params=params) as resp:
            resp.raise_for_status()
            data: dict = await resp.json()

        tracks = [self._parse_track(t) for t in data.get("tracks", [])]

        return SearchResult(
            tracks=tracks,
            total=int(data.get("total", len(tracks))),
            page=int(data.get("page", page)),
            has_next=bool(data.get("has_next", False)),
            source_name=self.name,
            query=query,
        )

    def _parse_track(self, raw: dict) -> Track:
        """
        Конвертирует raw-словарь из API-ответа в унифицированный Track.

        Адаптируй имена полей под реальный API своего сервиса.
        """
        return Track(
            title=str(raw.get("title", "Unknown")),
            artist=str(raw.get("artist", "")),
            duration=int(raw.get("duration", 0)),
            size=int(raw.get("size", 0)),
            bitrate=int(raw.get("bitrate", 0)),
            is_lossless=bool(raw.get("is_lossless", False)),
            thumbnail_url=str(raw.get("thumbnail_url", "")),
            # source_track_id хранит то, что нужно для get_audio():
            # у HTTP-источников это обычно прямой URL или ID трека
            source_track_id=str(raw.get("audio_url") or raw.get("id", "")),
            raw=raw,
        )

    # ── Получение аудио ───────────────────────────────────────────────────────

    async def get_audio(self, track: Track) -> AudioFile:
        """
        Получает аудиофайл трека.

        Для HTTP-источника source_track_id — это либо прямой URL (audio_url),
        либо ID трека для запроса отдельного эндпоинта /tracks/{id}/download.

        В реальном боте этот метод должен:
          1. Скачать файл во временный путь, ИЛИ
          2. Передать Telegram прямую ссылку через input_file_url
        Здесь мы возвращаем AudioFile с file_path для дальнейшей обработки.
        """
        start = time.monotonic()
        try:
            audio = await self._get_audio_internal(track)
            self.record_success((time.monotonic() - start) * 1000)
            return audio
        except TrackNotFoundError:
            self.record_error()
            raise
        except Exception as e:
            self.record_error()
            raise SourceUnavailableError(f"Ошибка получения аудио: {e}") from e

    async def _get_audio_internal(self, track: Track) -> AudioFile:
        if not track.source_track_id:
            raise TrackNotFoundError(
                f"Нет source_track_id у трека: {track.title!r}"
            )

        # Если source_track_id — это прямой URL, используем его
        if track.source_track_id.startswith(("http://", "https://")):
            audio_url = track.source_track_id
        else:
            # Иначе запрашиваем URL у API
            audio_url = await self._fetch_download_url(track.source_track_id)

        # Проверяем доступность URL (HEAD-запрос без скачивания)
        session = await self._get_session()
        try:
            async with session.head(audio_url, allow_redirects=True) as resp:
                if resp.status == 404:
                    raise TrackNotFoundError(
                        f"Аудиофайл не найден: {track.title!r}"
                    )
                resp.raise_for_status()
                content_length = int(resp.headers.get("Content-Length", track.size))
        except aiohttp.ClientError as e:
            raise SourceUnavailableError(f"Ошибка HEAD-запроса: {e}") from e

        # Возвращаем AudioFile с file_path = URL
        # В боте можно использовать как: bot.send_audio(chat_id, audio=audio_url)
        return AudioFile(
            telegram_file_id="",           # заполнится после отправки в Telegram
            telegram_unique_id="",         # заполнится после отправки в Telegram
            title=track.title,
            artist=track.artist,
            duration=track.duration,
            size=content_length,
            file_path=audio_url,           # прямой URL для отправки
        )

    async def _fetch_download_url(self, track_id: str) -> str:
        """Запрашивает прямой URL скачивания у API по ID трека."""
        session = await self._get_session()
        try:
            async with session.get(f"/tracks/{track_id}/download") as resp:
                resp.raise_for_status()
                data: dict = await resp.json()
                url = data.get("url") or data.get("download_url")
                if not url:
                    raise TrackNotFoundError(
                        f"API не вернул download_url для трека {track_id!r}"
                    )
                return str(url)
        except aiohttp.ClientResponseError as e:
            if e.status == 404:
                raise TrackNotFoundError(f"Трек {track_id!r} не найден") from e
            raise SourceUnavailableError(str(e)) from e

    # ── Health Check ──────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """
        Проверяет доступность API.
        Ожидает эндпоинт GET /health или /ping, возвращающий 200 OK.
        Адаптируй под реальный эндпоинт своего сервиса.
        """
        try:
            session = await self._get_session()
            # Пробуем /health, при 404 пробуем корневой URL
            for endpoint in ("/health", "/ping", "/"):
                try:
                    async with session.get(endpoint) as resp:
                        if resp.status < 500:
                            logger.debug(
                                "Health check %s %s → %d",
                                self.name, endpoint, resp.status,
                            )
                            return resp.status < 400
                except Exception:
                    continue
            return False
        except Exception as e:
            logger.warning("Health check %s failed: %s", self.name, e)
            return False

    # ── Cleanup ───────────────────────────────────────────────────────────────

    def __del__(self) -> None:
        """Закрываем сессию при удалении объекта (best-effort)."""
        if self._session and not self._session.closed:
            # Нельзя await в __del__, логируем предупреждение
            logger.warning(
                "CustomMusicSource: сессия не была закрыта явно. "
                "Используй await source.close() при остановке."
            )

    async def close(self) -> None:
        """Явно закрывает HTTP-сессию. Вызывать при остановке воркера."""
        await self._close_session()

    def __repr__(self) -> str:
        return (
            f"<CustomMusicSource "
            f"url={self._base_url!r} "
            f"priority={self.priority} "
            f"enabled={self.enabled}>"
        )
