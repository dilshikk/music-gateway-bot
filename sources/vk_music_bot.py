import asyncio
import hashlib
import logging
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

logger = logging.getLogger(__name__)


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

    logger.debug("[parse] msg_id=%d text_len=%d has_markup=%s",
                 msg.id, len(text), bool(msg.reply_markup))

    total_match = re.search(r"Результаты\s+(\d+)-(\d+)\s+из\s+(\d+)", text)
    if total_match:
        start = int(total_match.group(1))
        end   = int(total_match.group(2))
        total = int(total_match.group(3))
        page  = (start - 1) // (end - start + 1) + 1
        has_next = end < total
        logger.debug("[parse] результаты %d–%d из %d  page=%d has_next=%s",
                     start, end, total, page, has_next)
    else:
        logger.warning("[parse] строка 'Результаты ...' не найдена в тексте сообщения")

    button_map: dict[int, str] = {}
    # Также сохраняем плоский индекс кнопки в клавиатуре — нужен для click(int)
    button_index_map: dict[int, int] = {}  # номер трека → плоский индекс кнопки
    flat_index = 0
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text.isdigit() and btn.callback_data:
                    track_num = int(btn.text)
                    button_map[track_num] = btn.callback_data
                    button_index_map[track_num] = flat_index
                    logger.debug("[parse] кнопка #%s → flat_index=%d  callback_data=%r",
                                 btn.text, flat_index, btn.callback_data)
                flat_index += 1
    logger.debug("[parse] всего кнопок с треками: %d", len(button_map))

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

        cbd = button_map.get(num, "")
        btn_idx = button_index_map.get(num, -1)
        logger.debug(
            "[parse] #%d  artist=%r  title=%r  dur=%ds  size=%.1fMB  "
            "bitrate=%dk  lossless=%s  btn_index=%d  callback_data=%r",
            num, artist.strip(), title.strip(), duration, size_mb,
            bitrate, lossless, btn_idx, cbd,
        )

        tracks.append(Track(
            title=title.strip(),
            artist=artist.strip(),
            duration=duration,
            size=int(size_mb * 1024 * 1024),
            bitrate=bitrate,
            is_lossless=lossless,
            source_track_id=cbd,
            raw={"button_num": num, "callback_data": cbd, "button_index": btn_idx},
        ))

    logger.debug("[parse] итог: %d треков распаршено", len(tracks))
    return _ParsedResult(tracks=tracks, total=total, page=page, has_next=has_next)


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
      3. Нажать кнопку нужного трека через click(плоской_индекс)
      4. Дождаться сообщения с аудио
      5a. Если target_chat_id задан: userbot пересылает аудио напрямую (already_sent=True)
      5b. Иначе: возвращаем file_id для пересылки ботом
    """

    name         = "VK Music Bot"
    bot_username = "vkmusic_bot"
    source_type  = "telegram_bot"

    SEARCH_WAIT     = 12.0
    AUDIO_WAIT      = 20.0
    POLL_INTERVAL   = 0.5

    def __init__(
        self,
        client: Client,
        priority: int = 1,
        timeout: int = 30,
        enabled: bool = True,
    ) -> None:
        super().__init__(priority=priority, timeout=timeout, enabled=enabled)
        self._client = client

    # ── Поиск ───────────────────────────────────────────────────────────────────────

    async def search(self, query: str, page: int = 1) -> SearchResult:
        logger.info("[search] начало  query=%r  page=%d", query, page)
        start = time.monotonic()
        try:
            result = await self._search_internal(query, page)
            elapsed = (time.monotonic() - start) * 1000
            self.record_success(elapsed)
            logger.info(
                "[search] успех  query=%r  найдено=%d  всего=%d  за=%.0fms",
                query, len(result.tracks), result.total, elapsed,
            )
            return result
        except (SourceFloodWaitError, SourceTimeoutError, SourceUnavailableError) as e:
            self.record_error()
            logger.error("[search] известная ошибка  query=%r  %s: %s",
                         query, type(e).__name__, e)
            raise
        except FloodWait as e:
            self.record_error()
            logger.warning("[search] FloodWait %ds  query=%r", e.value, query)
            raise SourceFloodWaitError(e.value) from e
        except Exception as e:
            self.record_error()
            logger.exception("[search] неожиданная ошибка  query=%r", query)
            raise SourceUnavailableError(str(e)) from e

    async def _search_internal(self, query: str, page: int) -> SearchResult:
        prev_id = await self._get_last_message_id()
        logger.debug("[search_internal] prev_id=%d  отправляем запрос=%r", prev_id, query)

        await self._client.send_message(self.bot_username, query)
        logger.debug("[search_internal] сообщение отправлено  ждём ответ с кнопками (timeout=%.1fs)",
                     self.SEARCH_WAIT)

        msg = await self._wait_for_reply(
            prev_id=prev_id,
            has_markup=True,
            timeout=self.SEARCH_WAIT,
        )
        if not msg:
            logger.error("[search_internal] таймаут: бот не ответил за %.1fs", self.SEARCH_WAIT)
            raise SourceTimeoutError(f"Нет ответа от {self.bot_username}")

        logger.debug("[search_internal] получен ответ  msg_id=%d", msg.id)
        parsed = _parse_search_message(msg)

        if page > 1:
            logger.info("[search_internal] нужна страница %d, листаем...", page)
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

    async def get_audio(
        self,
        track: Track,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        logger.info(
            "[get_audio] начало  artist=%r  title=%r  "
            "source_track_id=%r  btn_index=%s  target_chat_id=%s",
            track.artist, track.title, track.source_track_id,
            track.raw.get("button_index"), target_chat_id,
        )
        start = time.monotonic()
        try:
            audio = await self._get_audio_internal(track, target_chat_id)
            elapsed = (time.monotonic() - start) * 1000
            self.record_success(elapsed)
            logger.info(
                "[get_audio] успех  file_id=%r  already_sent=%s  за=%.0fms",
                audio.telegram_file_id, audio.already_sent, elapsed,
            )
            return audio
        except FloodWait as e:
            self.record_error()
            logger.warning("[get_audio] FloodWait %ds  track=%r", e.value, track.title)
            raise SourceFloodWaitError(e.value) from e
        except TrackNotFoundError as e:
            self.record_error()
            logger.error("[get_audio] трек не найден: %s", e)
            raise
        except Exception as e:
            self.record_error()
            logger.exception("[get_audio] неожиданная ошибка  track=%r", track.title)
            raise SourceUnavailableError(str(e)) from e

    async def _get_audio_internal(
        self,
        track: Track,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        if not track.source_track_id:
            logger.error("[get_audio_internal] source_track_id пустой для %r", track.title)
            raise TrackNotFoundError(
                f"Трек не имеет source_track_id: {track.title}"
            )

        # Плоский индекс кнопки в клавиатуре, сохранённый при парсинге
        btn_index: int = track.raw.get("button_index", -1)
        if btn_index < 0:
            # Fallback: button_num - 1 (digit buttons always come first in VK bot keyboard)
            # Это нужно для треков из кэша, где button_index ещё не был сохранён
            fallback = track.raw.get("button_num")
            if isinstance(fallback, int) and fallback > 0:
                btn_index = fallback - 1
                logger.warning(
                    "[get_audio_internal] button_index отсутствует, "
                    "используем fallback button_num-1=%d для %r",
                    btn_index, track.title,
                )
            else:
                logger.error(
                    "[get_audio_internal] button_index не задан для %r  raw=%r",
                    track.title, track.raw,
                )
                raise TrackNotFoundError(
                    f"Нет button_index для трека: {track.title}"
                )

        logger.debug("[get_audio_internal] ищём сообщение с кнопками в истории чата")
        search_msg = await self._get_last_search_message()
        if not search_msg:
            logger.error("[get_audio_internal] не найдено сообщение с результатами (limit=5)")
            raise TrackNotFoundError("Не найдено сообщение с результатами поиска")

        logger.debug("[get_audio_internal] нашли search_msg_id=%d", search_msg.id)

        # Запоминаем prev_id ДО нажатия кнопки
        prev_id = await self._get_last_message_id()
        logger.debug(
            "[get_audio_internal] prev_id=%d  нажимаем кнопку btn_index=%d",
            prev_id, btn_index,
        )

        # BUG FIX: click(str) ищет по тексту кнопки, request_callback_answer требует
        # расшифрованные данные и даёт DATA_INVALID.
        # click(int) нажимает по плоскому индексу в клавиатуре — работает всегда.
        await search_msg.click(btn_index)
        logger.debug("[get_audio_internal] click(%d) отправлен  ждём аудио (timeout=%.1fs)",
                     btn_index, self.AUDIO_WAIT)

        audio_msg = await self._wait_for_audio(prev_id=prev_id, timeout=self.AUDIO_WAIT)
        if not audio_msg or not audio_msg.audio:
            logger.error(
                "[get_audio_internal] аудио не пришло за %.1fs  audio_msg=%s",
                self.AUDIO_WAIT,
                f"id={audio_msg.id} has_audio={bool(audio_msg.audio)}" if audio_msg else "None",
            )
            raise TrackNotFoundError(f"Аудио не получено для трека: {track.title}")

        a = audio_msg.audio
        logger.debug(
            "[get_audio_internal] аудио получено  msg_id=%d  file_id=%r  "
            "performer=%r  title=%r  duration=%ds  size=%dB",
            audio_msg.id, a.file_id, a.performer, a.title, a.duration or 0, a.file_size or 0,
        )

        if target_chat_id:
            logger.info(
                "[get_audio_internal] пересылаем аудио в чат пользователя  "
                "target_chat_id=%d  from_chat=%s  msg_id=%d",
                target_chat_id, self.bot_username, audio_msg.id,
            )
            sent = await self._client.copy_message(
                chat_id=target_chat_id,
                from_chat_id=self.bot_username,
                message_id=audio_msg.id,
            )
            logger.info(
                "[get_audio_internal] аудио отправлено пользователю  "
                "sent_msg_id=%d  target_chat_id=%d",
                sent.id, target_chat_id,
            )
            sent_audio = sent.audio
            return AudioFile(
                telegram_file_id=sent_audio.file_id if sent_audio else a.file_id,
                telegram_unique_id=sent_audio.file_unique_id if sent_audio else a.file_unique_id,
                title=sent_audio.title if sent_audio else (a.title or track.title),
                artist=sent_audio.performer if sent_audio else (a.performer or track.artist),
                duration=sent_audio.duration if sent_audio else (a.duration or track.duration),
                size=sent_audio.file_size if sent_audio else (a.file_size or track.size),
                already_sent=True,
            )

        return AudioFile(
            telegram_file_id=a.file_id,
            telegram_unique_id=a.file_unique_id,
            title=a.title or track.title,
            artist=a.performer or track.artist,
            duration=a.duration or track.duration,
            size=a.file_size or track.size,
            already_sent=False,
        )

    # ── Health Check ─────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        logger.debug("[health_check] проверяем @%s", self.bot_username)
        try:
            chat = await self._client.get_chat(self.bot_username)
            ok = chat is not None
            logger.info("[health_check] @%s → %s", self.bot_username, "OK" if ok else "FAIL")
            return ok
        except Exception as e:
            logger.warning("[health_check] @%s → FAIL: %s", self.bot_username, e)
            return False

    # ── Навигация по страницам ──────────────────────────────────────────────

    async def _navigate_to_page(self, msg: Message, target_page: int) -> Message:
        """Листает страницы через кнопку ➡️ до нужной страницы."""
        logger.info("[navigate] листаем с стр. 1 до стр. %d", target_page)
        current = msg
        for step in range(target_page - 1):
            logger.debug("[navigate] шаг %d/%d  current_msg_id=%d",
                         step + 1, target_page - 1, current.id)

            # Находим плоский индекс кнопки ➡️
            next_btn_index = _find_button_flat_index(current, "\u27a1\ufe0f")
            if next_btn_index < 0:
                logger.warning("[navigate] кнопка ➡️ не найдена на шаге %d", step + 1)
                break

            logger.debug("[navigate] нажимаем ➡️  flat_index=%d", next_btn_index)
            prev_id = await self._get_last_message_id()
            await current.click(next_btn_index)
            logger.debug("[navigate] click(%d) отправлен  prev_id=%d  ждём ответ...",
                         next_btn_index, prev_id)

            updated = await self._wait_for_reply(
                prev_id=prev_id,
                has_markup=True,
                timeout=self.SEARCH_WAIT,
            )
            if updated:
                logger.debug("[navigate] новая страница получена  msg_id=%d", updated.id)
                current = updated
            else:
                logger.warning("[navigate] таймаут на шаге %d", step + 1)
                break

        logger.info("[navigate] закончено  итоговый msg_id=%d", current.id)
        return current

    # ── Вспомогательные методы ────────────────────────────────────────────

    async def _get_last_message_id(self) -> int:
        async for m in self._client.get_chat_history(self.bot_username, limit=1):
            logger.debug("[get_last_msg_id] last_id=%d", m.id)
            return m.id
        logger.warning("[get_last_msg_id] история чата пустая, возвращаем 0")
        return 0

    async def _wait_for_reply(
        self,
        prev_id: int,
        has_markup: bool = False,
        timeout: float = 12.0,
    ) -> Message | None:
        logger.debug("[wait_reply] начало  prev_id=%d  has_markup=%s  timeout=%.1fs",
                     prev_id, has_markup, timeout)
        deadline = time.monotonic() + timeout
        last_seen_id = prev_id
        poll_count = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll_count += 1
            elapsed = timeout - (deadline - time.monotonic())
            logger.debug("[wait_reply] опрос #%d  elapsed=%.1fs  last_seen_id=%d",
                         poll_count, elapsed, last_seen_id)

            async for m in self._client.get_chat_history(self.bot_username, limit=3):
                logger.debug(
                    "[wait_reply]   msg_id=%d  has_text=%s  has_markup=%s  is_new=%s",
                    m.id, bool(m.text), bool(m.reply_markup), m.id > last_seen_id,
                )
                if m.id <= last_seen_id:
                    break
                if has_markup and not m.reply_markup:
                    logger.debug(
                        "[wait_reply]   msg_id=%d — новое, но без кнопок, пропускаем  text=%r",
                        m.id, (m.text or "")[:80],
                    )
                    last_seen_id = m.id
                    continue
                logger.info("[wait_reply] нашли подходящее сообщение  msg_id=%d  has_markup=%s",
                            m.id, bool(m.reply_markup))
                return m

        logger.warning("[wait_reply] таймаут %.1fs  poll_count=%d  last_seen_id=%d",
                       timeout, poll_count, last_seen_id)
        return None

    async def _wait_for_audio(self, prev_id: int, timeout: float = 20.0) -> Message | None:
        logger.debug("[wait_audio] начало  prev_id=%d  timeout=%.1fs", prev_id, timeout)
        deadline = time.monotonic() + timeout
        poll_count = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll_count += 1
            elapsed = timeout - (deadline - time.monotonic())
            logger.debug("[wait_audio] опрос #%d  elapsed=%.1fs", poll_count, elapsed)

            async for m in self._client.get_chat_history(self.bot_username, limit=5):
                logger.debug(
                    "[wait_audio]   msg_id=%d  has_audio=%s  has_text=%s  "
                    "has_caption=%s  id>prev=%s",
                    m.id, bool(m.audio), bool(m.text), bool(m.caption), m.id > prev_id,
                )
                if m.id > prev_id and m.audio:
                    logger.info(
                        "[wait_audio] аудио найдено  msg_id=%d  "
                        "file_id=%r  performer=%r  title=%r",
                        m.id, m.audio.file_id, m.audio.performer, m.audio.title,
                    )
                    return m

        logger.warning("[wait_audio] таймаут %.1fs  poll_count=%d", timeout, poll_count)
        return None

    async def _get_last_search_message(self) -> Message | None:
        logger.debug("[get_last_search_msg] ищем сообщение с кнопками (limit=5)")
        async for m in self._client.get_chat_history(self.bot_username, limit=5):
            has_markup = bool(m.reply_markup)
            logger.debug("[get_last_search_msg]   msg_id=%d  has_markup=%s", m.id, has_markup)
            if has_markup:
                logger.debug("[get_last_search_msg] нашли  msg_id=%d", m.id)
                return m
        logger.warning("[get_last_search_msg] сообщение с кнопками не найдено")
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────

def _find_button_flat_index(msg: Message, text: str) -> int:
    """
    Возвращает плоский индекс (0-based) кнопки с заданным текстом.
    -1 если не найдена.
    """
    if not msg.reply_markup:
        return -1
    idx = 0
    for row in msg.reply_markup.inline_keyboard:
        for btn in row:
            if btn.text == text:
                return idx
            idx += 1
    return -1


def _make_query_hash(query: str) -> str:
    """MD5 от нормализованного запроса — ключ кэша."""
    normalized = query.strip().lower()
    return hashlib.md5(normalized.encode()).hexdigest()
