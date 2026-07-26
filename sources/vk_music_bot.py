"""
Источник музыки через @vkmusic_bot (Pyrogram userbot).

BUG FIX (race condition):
    Раньше last_id запоминался ПОСЛЕ send_message. Если @vkmusic_bot
    успевал ответить раньше первого опроса, ответ уже был в истории —
    его last_id становился «базовым», и новых сообщений не появлялось.
    Теперь last_id фиксируется ДО отправки запроса.
"""
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

# ─── Парсинг ответа бота ──────────────────────────────────────────────────────

@dataclass
class _ParsedResult:
    tracks: list[Track]
    total: int
    page: int
    has_next: bool


def _parse_search_message(msg: Message) -> _ParsedResult:
    text = msg.text or ""
    tracks: list[Track] = []
    total = 0
    page = 1
    has_next = False

    total_match = re.search(r"Результаты\s+(\d+)-(\d+)\s+из\s+(\d+)", text)
    if total_match:
        start = int(total_match.group(1))
        end   = int(total_match.group(2))
        total = int(total_match.group(3))
        page  = (start - 1) // (end - start + 1) + 1
        has_next = end < total

    button_map: dict[int, str] = {}
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text.isdigit() and btn.callback_data:
                    button_map[int(btn.text)] = btn.callback_data

    line_pattern = re.compile(
        r"^(\d+)\.\s+(.+?)\s+"
        r"(\d+:\d{2})\s+"
        r"([\d.]+)M\s+"
        r"(\d+)k"
        r"(\s+Lossless)?",
        re.MULTILINE,
    )

    for m in line_pattern.finditer(text):
        num       = int(m.group(1))
        raw_title = m.group(2).strip()
        duration  = _parse_duration(m.group(3))
        size_mb   = float(m.group(4))
        bitrate   = int(m.group(5))
        lossless  = bool(m.group(6))

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

    return _ParsedResult(tracks=tracks, total=total, page=page, has_next=has_next)


def _parse_duration(s: str) -> int:
    parts = s.split(":")
    if len(parts) == 2:
        return int(parts[0]) * 60 + int(parts[1])
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    return 0


# ─── Источник ─────────────────────────────────────────────────────────────────

class VKMusicBotSource(MusicSource):
    """
    Источник музыки через @vkmusic_bot.

    Доставка аудио:
        Userbot получает аудио-сообщение от @vkmusic_bot, затем копирует его
        напрямую в чат пользователя через copy_message (если передан
        target_chat_id).  Главный бот answer_audio в этом случае НЕ вызывается,
        т.к. file_id из Pyrogram-сессии невалиден для другой сессии/токена.
    """

    name = "VK Music Bot"
    bot_username = "vkmusic_bot"
    source_type = "telegram_bot"

    SEARCH_WAIT   = 15.0   # увеличен с 5 → 15: сервер может быть медленным
    AUDIO_WAIT    = 20.0   # увеличен с 10 → 20
    POLL_INTERVAL = 0.3    # уменьшен с 0.5 → 0.3: реагируем быстрее

    def __init__(
        self,
        client: Client,
        priority: int = 1,
        timeout: int = 30,
        enabled: bool = True,
    ) -> None:
        super().__init__(priority=priority, timeout=timeout, enabled=enabled)
        self._client = client

    # ── Поиск ─────────────────────────────────────────────────────────────────

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
        # BUG FIX: запоминаем last_id ДО отправки запроса.
        # Если фиксировать после — @vkmusic_bot успевает ответить быстрее
        # первого опроса, ответ уже в истории, и _wait_for_reply его пропускает.
        last_id = await self._get_last_message_id()

        await self._client.send_message(self.bot_username, query)

        msg = await self._wait_for_new_reply(
            after_id=last_id,
            has_markup=True,
            timeout=self.SEARCH_WAIT,
        )
        if not msg:
            raise SourceTimeoutError(f"Нет ответа от {self.bot_username}")

        parsed = _parse_search_message(msg)

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

    # ── Получение / доставка аудио ────────────────────────────────────────────

    async def get_audio(
        self,
        track: Track,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        start = time.monotonic()
        try:
            audio = await self._get_audio_internal(track, target_chat_id)
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

    async def _get_audio_internal(
        self,
        track: Track,
        target_chat_id: int | None,
    ) -> AudioFile:
        if not track.source_track_id:
            raise TrackNotFoundError(
                f"Трек не имеет source_track_id: {track.title}"
            )

        search_msg = await self._get_last_search_message()
        if not search_msg:
            raise TrackNotFoundError("Не найдено сообщение с результатами поиска")

        # BUG FIX: фиксируем last_id ДО нажатия кнопки (та же логика что и в поиске)
        last_id = await self._get_last_message_id()

        clicked = await _click_by_callback_data(search_msg, track.source_track_id)
        if not clicked:
            raise TrackNotFoundError(
                f"Кнопка с callback_data '{track.source_track_id}' не найдена"
            )

        audio_msg = await self._wait_for_new_audio(
            after_id=last_id,
            timeout=self.AUDIO_WAIT,
        )
        if not audio_msg or not audio_msg.audio:
            raise TrackNotFoundError(f"Аудио не получено для трека: {track.title}")

        delivered = False
        if target_chat_id is not None:
            await self._client.copy_message(
                chat_id=target_chat_id,
                from_chat_id=audio_msg.chat.id,
                message_id=audio_msg.id,
            )
            delivered = True

        return AudioFile(
            telegram_file_id=audio_msg.audio.file_id,
            telegram_unique_id=audio_msg.audio.file_unique_id,
            title=audio_msg.audio.title or track.title,
            artist=audio_msg.audio.performer or track.artist,
            duration=audio_msg.audio.duration or track.duration,
            size=audio_msg.audio.file_size or track.size,
            delivered=delivered,
        )

    # ── Health Check ──────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            chat = await self._client.get_chat(self.bot_username)
            return chat is not None
        except Exception:
            return False

    # ── Навигация по страницам ────────────────────────────────────────────────

    async def _navigate_to_page(self, msg: Message, target_page: int) -> Message:
        current = msg
        for _ in range(target_page - 1):
            next_btn = _find_button_by_text(current, "➡️")
            if not next_btn:
                break
            last_id = await self._get_last_message_id()
            await _click_by_callback_data(current, next_btn.callback_data)
            updated = await self._wait_for_new_reply(
                after_id=last_id,
                has_markup=True,
                timeout=self.SEARCH_WAIT,
            )
            if updated:
                current = updated
        return current

    # ── Вспомогательные методы ────────────────────────────────────────────────

    async def _get_last_message_id(self) -> int:
        """Возвращает ID последнего сообщения в диалоге с ботом."""
        async for m in self._client.get_chat_history(self.bot_username, limit=1):
            return m.id
        return 0

    async def _wait_for_new_reply(
        self,
        after_id: int,
        has_markup: bool = False,
        timeout: float = 15.0,
    ) -> Message | None:
        """
        Ждёт сообщение с ID > after_id.
        after_id должен быть зафиксирован ДО действия (send_message / click).
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            async for m in self._client.get_chat_history(self.bot_username, limit=3):
                if m.id <= after_id:
                    break  # история отсортирована по убыванию — дальше старые
                if has_markup and not m.reply_markup:
                    continue
                return m
        return None

    async def _wait_for_new_audio(
        self,
        after_id: int,
        timeout: float = 20.0,
    ) -> Message | None:
        """Ждёт аудио-сообщение с ID > after_id."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            async for m in self._client.get_chat_history(self.bot_username, limit=5):
                if m.id <= after_id:
                    break
                if m.audio:
                    return m
        return None

    async def _get_last_search_message(self) -> Message | None:
        """Последнее сообщение с inline-кнопками (результаты поиска)."""
        async for m in self._client.get_chat_history(self.bot_username, limit=5):
            if m.reply_markup:
                return m
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────────

async def _click_by_callback_data(msg: Message, callback_data: str) -> bool:
    """
    Нажимает кнопку по значению callback_data (не по тексту).
    Pyrogram.Message.click() по умолчанию ищет по тексту — это не работает
    для кнопок вида 'a:5192961137011854851:1'.
    """
    if not msg.reply_markup:
        return False

    for row_idx, row in enumerate(msg.reply_markup.inline_keyboard):
        for col_idx, btn in enumerate(row):
            if btn.callback_data == callback_data:
                try:
                    await msg.click(row_idx, col_idx)
                except TypeError:
                    await msg.click(btn.text)
                return True

    return False


def _find_button_by_text(msg: Message, text: str):
    if not msg.reply_markup:
        return None
    for row in msg.reply_markup.inline_keyboard:
        for btn in row:
            if btn.text == text:
                return btn
    return None


def _make_query_hash(query: str) -> str:
    normalized = query.strip().lower()
    return hashlib.md5(normalized.encode()).hexdigest()
