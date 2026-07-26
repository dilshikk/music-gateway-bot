import asyncio
import hashlib
import re
import time
from dataclasses import dataclass

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

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


# ─── Парсинг ответа бота ───────────────────────────────────────────────

@dataclass
class _ParsedResult:
    tracks: list[Track]
    total: int
    page: int
    has_next: bool


def _parse_search_message(msg: Message) -> _ParsedResult:
    """
    Парсит текст ответа @vkmusic_bot вида:

    🔍 Mulk
    Результаты 1-8 из 1000

    1. Artist - Title  48:32  44.4M  128k
    ...

    И inline-кнопки для получения callback_data каждого трека.
    """
    text = msg.text or ""
    tracks: list[Track] = []
    total = 0
    page = 1
    has_next = False

    # Общее кол-во результатов: "Результаты 1-8 из 1000"
    total_match = re.search(r"Результаты\s+(\d+)-(\d+)\s+из\s+(\d+)", text)
    if total_match:
        start = int(total_match.group(1))
        end   = int(total_match.group(2))
        total = int(total_match.group(3))
        page  = (start - 1) // (end - start + 1) + 1
        has_next = end < total

    # Кнопки: каждая кнопка с числом (1-8) содержит callback_data трека
    button_map: dict[int, str] = {}  # номер → callback_data
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text.isdigit() and btn.callback_data:
                    button_map[int(btn.text)] = btn.callback_data

    # Строки треков: "1. Artist - Title  HH:MM  XXM  128k"
    line_pattern = re.compile(
        r"^(\d+)\.\s+(.+?)\s+"             # номер + название
        r"(\d+:\d{2})\s+"                   # длительность MM:SS или HH:MM:SS
        r"([\d.]+)M\s+"                     # размер в МБ
        r"(\d+)k"                           # битрейт
        r"(\s+Lossless)?",                  # опциональный Lossless
        re.MULTILINE,
    )

    for m in line_pattern.finditer(text):
        num       = int(m.group(1))
        raw_title = m.group(2).strip()
        duration  = _parse_duration(m.group(3))
        size_mb   = float(m.group(4))
        bitrate   = int(m.group(5))
        lossless  = bool(m.group(6))

        # Разбиваем "Artist - Title" если есть разделитель
        if " - " in raw_title:
            artist, title = raw_title.split(" - ", 1)
        else:
            artist, title = "", raw_title

        tracks.append(Track(
            title=title.strip(),
            artist=artist.strip(),
            duration=duration,
            size=int(size_mb * 1024 * 1024),
            bitrate=bitrate,
            is_lossless=lossless,
            source_track_id=button_map.get(num, ""),
            raw={"button_num": num, "callback_data": button_map.get(num, "")},
        ))

    return _ParsedResult(
        tracks=tracks,
        total=total,
        page=page,
        has_next=has_next,
    )


def _parse_duration(s: str) -> int:
    """'48:32' → 2912, '1:02:30' → 3750"""
    parts = s.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


# ─── Источник ───────────────────────────────────────────────────────────────────

class VKMusicBotSource(MusicSource):
    """
    Источник музыки через @vkmusic_bot.

    Алгоритм:
      1. Отправить текстовый запрос боту
      2. Дождаться ответа с inline-кнопками
      3. Нажать кнопку нужного трека (click callback_data)
      4. Дождаться сообщения с аудио
      5. Вернуть file_id для пересылки
    """

    name         = "VK Music Bot"
    bot_username = "vkmusic_bot"
    source_type  = "telegram_bot"

    # Задержки (секунды)
    # BUG FIX: увеличены таймауты: 5.0 и 10.0 было слишком мало
    # при медленном ответе бота или высокой нагрузке сервера
    SEARCH_WAIT     = 12.0  # ждём ответ на поисковый запрос
    AUDIO_WAIT      = 20.0  # ждём ответ после нажатия кнопки
    POLL_INTERVAL   = 0.5   # интервал polling при ожидании

    def __init__(
        self,
        client: Client,  # Pyrogram userbot
        priority: int = 1,
        timeout: int = 30,
        enabled: bool = True,
    ) -> None:
        super().__init__(priority=priority, timeout=timeout, enabled=enabled)
        self._client = client

    # ── Поиск ───────────────────────────────────────────────────────────────────────

    async def search(self, query: str, page: int = 1) -> SearchResult:
        start = time.monotonic()
        try:
            result = await self._search_internal(query, page)
            self.record_success((time.monotonic() - start) * 1000)
            return result
        except (SourceFloodWaitError, SourceTimeoutError, SourceUnavailableError):
            self.record_error()
            raise
        except FloodWait as e:
            self.record_error()
            raise SourceFloodWaitError(e.value) from e
        except Exception as e:
            self.record_error()
            raise SourceUnavailableError(str(e)) from e

    async def _search_internal(self, query: str, page: int) -> SearchResult:
        # BUG FIX: запоминаем last_id ДО отправки запроса, а не внутри _wait_for_reply.
        # Если бот ответил очень быстро, внутренний get_chat_history в _wait_for_reply
        # мог увидеть уже ответ и зафиксировать его как last_id, игнорируя в петле.
        prev_id = await self._get_last_message_id()

        await self._client.send_message(self.bot_username, query)

        # Ждём ответ бота с inline-кнопками
        msg = await self._wait_for_reply(
            prev_id=prev_id,
            has_markup=True,
            timeout=self.SEARCH_WAIT,
        )
        if not msg:
            raise SourceTimeoutError(f"Нет ответа от {self.bot_username}")

        parsed = _parse_search_message(msg)

        # Если запрошена страница > 1 — листаем через ➡️ кнопку
        if page > 1:
            msg = await self._navigate_to_page(msg, page)
            parsed = _parse_search_message(msg)

        return SearchResult(
            tracks=parsed.tracks,
            total=parsed.total,
            page=parsed.page,
            has_next=parsed.has_next,
            source_name=self.name,
            query=query,
        )

    # ── Получение аудио ──────────────────────────────────────────────────────

    async def get_audio(self, track: Track) -> AudioFile:
        start = time.monotonic()
        try:
            audio = await self._get_audio_internal(track)
            self.record_success((time.monotonic() - start) * 1000)
            return audio
        except FloodWait as e:
            self.record_error()
            raise SourceFloodWaitError(e.value) from e
        except TrackNotFoundError:
            self.record_error()
            raise
        except Exception as e:
            self.record_error()
            raise SourceUnavailableError(str(e)) from e

    async def _get_audio_internal(self, track: Track) -> AudioFile:
        if not track.source_track_id:
            raise TrackNotFoundError(
                f"Трек не имеет source_track_id: {track.title}"
            )

        # Находим последнее сообщение с inline-кнопками (результаты поиска)
        search_msg = await self._get_last_search_message()
        if not search_msg:
            raise TrackNotFoundError("Не найдено сообщение с результатами поиска")

        # BUG FIX: запоминаем prev_id ДО нажатия кнопки.
        # Бот может отправить аудио очень быстро; если prev_id захватывать внутри _wait_for_audio,
        # то last_id может уже указывать на аудио-сообщение — и метод его пропустит.
        prev_id = await self._get_last_message_id()

        # Нажимаем кнопку с нужным callback_data
        await search_msg.click(track.source_track_id)

        # Ждём сообщение с аудио
        audio_msg = await self._wait_for_audio(prev_id=prev_id, timeout=self.AUDIO_WAIT)
        if not audio_msg or not audio_msg.audio:
            raise TrackNotFoundError(f"Аудио не получено для трека: {track.title}")

        return AudioFile(
            telegram_file_id=audio_msg.audio.file_id,
            telegram_unique_id=audio_msg.audio.file_unique_id,
            title=audio_msg.audio.title or track.title,
            artist=audio_msg.audio.performer or track.artist,
            duration=audio_msg.audio.duration or track.duration,
            size=audio_msg.audio.file_size or track.size,
        )

    # ── Health Check ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            chat = await self._client.get_chat(self.bot_username)
            return chat is not None
        except Exception:
            return False

    # ── Навигация по страницам ──────────────────────────────────────────────

    async def _navigate_to_page(self, msg: Message, target_page: int) -> Message:
        """Листает страницы через кнопку ➡️ до нужной страницы."""
        current = msg
        for _ in range(target_page - 1):
            next_btn = _find_button(current, "\u27a1\ufe0f")
            if not next_btn:
                break

            # BUG FIX: запоминаем prev_id ДО нажатия кнопки пагинации
            prev_id = await self._get_last_message_id()
            await current.click(next_btn.callback_data)
            updated = await self._wait_for_reply(
                prev_id=prev_id,
                has_markup=True,
                timeout=self.SEARCH_WAIT,
            )
            if updated:
                current = updated
        return current

    # ── Вспомогательные методы ────────────────────────────────────────────

    async def _get_last_message_id(self) -> int:
        """Возвращает ID последнего сообщения в чате с ботом. Используется как базовая линия."""
        async for m in self._client.get_chat_history(self.bot_username, limit=1):
            return m.id
        return 0

    async def _wait_for_reply(
        self,
        prev_id: int,
        has_markup: bool = False,
        timeout: float = 12.0,
    ) -> Message | None:
        """
        Ожидает новое сообщение от бота методом polling.

        prev_id — ID последнего сообщения ДО отправки запроса,
        захваченный вызывающим кодом (не внутри этого метода).

        BUG FIX: раньше last_id захватывался внутри этого метода уже после
        отправки сообщения. Если бот ответил до первого get_chat_history,
        ответ уже в истории — last_id фиксировался на нём и петля никогда
        не возвращала сообщение. Теперь prev_id передаётся снаружи.
        """
        deadline = time.monotonic() + timeout
        last_seen_id = prev_id

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            # BUG FIX: проверяем limit=3 чтобы не пропустить промежуточные сообщения
            async for m in self._client.get_chat_history(self.bot_username, limit=3):
                if m.id <= last_seen_id:
                    break
                # Новое сообщение
                if has_markup and not m.reply_markup:
                    # Промежуточное сообщение без кнопок — обновляем last_seen_id и продолжаем
                    last_seen_id = m.id
                    continue
                return m
        return None

    async def _wait_for_audio(self, prev_id: int, timeout: float = 20.0) -> Message | None:
        """
        Ждёт сообщение с аудио от бота.

        prev_id — ID последнего сообщения ДО нажатия кнопки.

        BUG FIX 1: раньше last_id захватывался внутри метода — аудио могло
        прийти до захвата и стать новым last_id, затем игнорироваться.
        BUG FIX 2: заменили проверку m.id != last_id на m.id > prev_id,
        чтобы перехватывать все новые сообщения, а не только первое следующее.
        """
        deadline = time.monotonic() + timeout

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            # BUG FIX: limit=5 чтобы перехватить несколько сообщений если бот
            # отправил несколько быстро — проверяем все до нахождения аудио
            async for m in self._client.get_chat_history(self.bot_username, limit=5):
                if m.id > prev_id and m.audio:
                    return m
        return None

    async def _get_last_search_message(self) -> Message | None:
        """Возвращает последнее сообщение с inline-кнопками (результаты поиска)."""
        async for m in self._client.get_chat_history(self.bot_username, limit=5):
            if m.reply_markup:
                return m
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────

def _find_button(msg: Message, text: str):
    """Находит кнопку по тексту в reply_markup."""
    if not msg.reply_markup:
        return None
    for row in msg.reply_markup.inline_keyboard:
        for btn in row:
            if btn.text == text:
                return btn
    return None


def _make_query_hash(query: str) -> str:
    """MD5 от нормализованного запроса — ключ кэша."""
    normalized = query.strip().lower()
    return hashlib.md5(normalized.encode()).hexdigest()
