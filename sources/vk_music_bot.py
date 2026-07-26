"""
VKMusicBotSource — источник музыки через @vkmusic_bot.

Архитектура (один userbot + asyncio.Queue):
  ┌─────────────┐     put()      ┌──────────┐    один за раз
  │ user A dl:  │ ─────────────► │          │
  │ user B dl:  │ ─────────────► │  Queue   │ ──► _worker ──► @vkmusic_bot
  │ user C dl:  │ ─────────────► │          │
  └─────────────┘                └──────────┘

Воркер обрабатывает задачи строго по одной — никакой гонки за ответами бота.
Каждая задача: нажать кнопку → дождаться аудио → скачать в RAM (BytesIO) →
отправить главному боту с caption="target:{user_chat_id}" → relay.py пересылает.
"""
import asyncio
import hashlib
import io
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

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

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB — лимит Telegram Bot API


# ─── Внутренние типы очереди ──────────────────────────────────────────────────

@dataclass
class _QueueItem:
    """Одна задача в очереди воркера."""
    coro: Any           # coroutine для выполнения
    future: asyncio.Future  # сюда кладём результат или исключение
    label: str = ""     # для отладки


# ─── Парсинг ответа бота ──────────────────────────────────────────────────────

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
        logger.warning("[parse] строка 'Результаты ...' не найдена")

    button_map: dict[int, str] = {}
    button_index_map: dict[int, int] = {}
    flat_index = 0
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text.isdigit() and btn.callback_data:
                    track_num = int(btn.text)
                    button_map[track_num] = btn.callback_data
                    button_index_map[track_num] = flat_index
                flat_index += 1
    logger.debug("[parse] кнопок с треками: %d", len(button_map))

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

        cbd     = button_map.get(num, "")
        btn_idx = button_index_map.get(num, -1)

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

    logger.debug("[parse] итог: %d треков", len(tracks))
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

    Все операции (поиск + скачивание) проходят через asyncio.Queue.
    Воркер обрабатывает их строго по одной — гарантирует изоляцию
    ответов @vkmusic_bot между разными пользователями.

    Поток скачивания:
      1. Нажать кнопку трека в чате @vkmusic_bot
      2. Дождаться аудио-сообщения
      3. Скачать файл в RAM (io.BytesIO), проверить ≤50MB
      4. Отправить главному боту: send_audio(bytes, caption="target:{chat_id}")
      5. relay.py читает caption → пересылает нужному пользователю
    """

    name         = "VK Music Bot"
    bot_username = "vkmusic_bot"
    source_type  = "telegram_bot"

    SEARCH_WAIT   = 12.0
    AUDIO_WAIT    = 20.0
    POLL_INTERVAL = 0.5

    def __init__(
        self,
        client: Client,
        priority: int = 1,
        timeout: int = 30,
        enabled: bool = True,
        relay_bot_id: int | None = None,
    ) -> None:
        super().__init__(priority=priority, timeout=timeout, enabled=enabled)
        self._client       = client
        self._relay_bot_id = relay_bot_id

        # Очередь задач — сердце новой архитектуры
        self._queue: asyncio.Queue[_QueueItem | None] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None

    # ── Жизненный цикл ────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Запускает воркер. Вызывать после инициализации клиента."""
        if self._worker_task and not self._worker_task.done():
            logger.warning("[source] воркер уже запущен")
            return
        self._worker_task = asyncio.create_task(self._worker(), name="vk-source-worker")
        logger.info("[source] воркер запущен")
        print("[source] воркер запущен")

    async def stop(self) -> None:
        """Останавливает воркер (sentinel None в очереди)."""
        await self._queue.put(None)
        if self._worker_task:
            try:
                await asyncio.wait_for(self._worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
        logger.info("[source] воркер остановлен")
        print("[source] воркер остановлен")

    # ── Воркер ────────────────────────────────────────────────────────────────

    async def _worker(self) -> None:
        """
        Главный цикл воркера.
        Берёт задачи из очереди по одной и выполняет их последовательно.
        None в очереди — сигнал остановки.
        """
        logger.info("[worker] старт  queue_size=%d", self._queue.qsize())
        print(f"[worker] старт")

        while True:
            item = await self._queue.get()

            # Sentinel — завершаем работу
            if item is None:
                logger.info("[worker] получен sentinel, останавливаемся")
                print("[worker] получен sentinel, останавливаемся")
                self._queue.task_done()
                break

            print(f"[worker] берём задачу: {item.label!r}  queue_remaining={self._queue.qsize()}")
            logger.debug("[worker] задача %r  queue_remaining=%d",
                         item.label, self._queue.qsize())

            try:
                result = await item.coro
                if not item.future.done():
                    item.future.set_result(result)
                print(f"[worker] задача {item.label!r} выполнена успешно")
            except Exception as e:
                if not item.future.done():
                    item.future.set_exception(e)
                print(f"[worker] задача {item.label!r} завершилась с ошибкой: {e}")
                logger.error("[worker] ошибка в задаче %r: %s", item.label, e)
            finally:
                self._queue.task_done()

    # ── Отправка задачи в очередь ─────────────────────────────────────────────

    async def _submit(self, coro: Any, label: str = "") -> Any:
        """Кладёт корутину в очередь и ждёт результата через Future."""
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = _QueueItem(coro=coro, future=future, label=label)
        await self._queue.put(item)
        pos = self._queue.qsize()
        print(f"[submit] задача {label!r} добавлена в очередь  позиция≈{pos}")
        logger.debug("[submit] задача %r добавлена  queue_size=%d", label, pos)
        return await future

    # ── Публичный API ─────────────────────────────────────────────────────────

    async def search(self, query: str, page: int = 1) -> SearchResult:
        label = f"search:{query[:30]}:p{page}"
        print(f"[search] submit  query={query!r}  page={page}")
        logger.info("[search] submit  query=%r  page=%d", query, page)
        start = time.monotonic()
        try:
            result = await self._submit(self._search_internal(query, page), label=label)
            elapsed = (time.monotonic() - start) * 1000
            self.record_success(elapsed)
            print(f"[search] готово  tracks={len(result.tracks)} total={result.total} за {elapsed:.0f}ms")
            logger.info("[search] готово  tracks=%d total=%d за %.0fms",
                        len(result.tracks), result.total, elapsed)
            return result
        except (SourceFloodWaitError, SourceTimeoutError, SourceUnavailableError) as e:
            self.record_error()
            print(f"[search] известная ошибка: {type(e).__name__}: {e}")
            raise
        except FloodWait as e:
            self.record_error()
            print(f"[search] FloodWait {e.value}s")
            raise SourceFloodWaitError(e.value) from e
        except Exception as e:
            self.record_error()
            print(f"[search] неожиданная ошибка: {e}")
            logger.exception("[search] неожиданная ошибка  query=%r", query)
            raise SourceUnavailableError(str(e)) from e

    async def get_audio(
        self,
        track: Track,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        label = f"audio:{track.artist[:20]}-{track.title[:20]}:chat{target_chat_id}"
        print(f"[get_audio] submit  track={track.title!r}  target_chat_id={target_chat_id}")
        logger.info("[get_audio] submit  track=%r  target_chat_id=%s",
                    track.title, target_chat_id)
        start = time.monotonic()
        try:
            audio = await self._submit(
                self._get_audio_internal(track, target_chat_id),
                label=label,
            )
            elapsed = (time.monotonic() - start) * 1000
            self.record_success(elapsed)
            print(f"[get_audio] готово  file_id={audio.telegram_file_id!r}  already_sent={audio.already_sent}  за {elapsed:.0f}ms")
            logger.info("[get_audio] готово  already_sent=%s  за %.0fms",
                        audio.already_sent, elapsed)
            return audio
        except FloodWait as e:
            self.record_error()
            print(f"[get_audio] FloodWait {e.value}s")
            raise SourceFloodWaitError(e.value) from e
        except TrackNotFoundError as e:
            self.record_error()
            print(f"[get_audio] TrackNotFoundError: {e}")
            raise
        except Exception as e:
            self.record_error()
            print(f"[get_audio] неожиданная ошибка: {e}")
            logger.exception("[get_audio] неожиданная ошибка  track=%r", track.title)
            raise SourceUnavailableError(str(e)) from e

    # ── Внутренние реализации (выполняются воркером) ──────────────────────────

    async def _search_internal(self, query: str, page: int) -> SearchResult:
        prev_id = await self._get_last_message_id()
        print(f"[_search_internal] prev_id={prev_id}  query={query!r}")
        logger.debug("[_search_internal] prev_id=%d  query=%r", prev_id, query)

        await self._client.send_message(self.bot_username, query)
        print(f"[_search_internal] сообщение отправлено  ждём ответ с кнопками...")

        msg = await self._wait_for_reply(prev_id=prev_id, has_markup=True, timeout=self.SEARCH_WAIT)
        if not msg:
            print(f"[_search_internal] таймаут: нет ответа за {self.SEARCH_WAIT}s")
            raise SourceTimeoutError(f"Нет ответа от {self.bot_username}")

        print(f"[_search_internal] ответ получен  msg_id={msg.id}")
        parsed = _parse_search_message(msg)

        if page > 1:
            print(f"[_search_internal] листаем до страницы {page}...")
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

    async def _get_audio_internal(
        self,
        track: Track,
        target_chat_id: int | None = None,
    ) -> AudioFile:
        if not track.source_track_id:
            print(f"[_get_audio_internal] source_track_id пустой для {track.title!r}")
            raise TrackNotFoundError(f"Трек не имеет source_track_id: {track.title}")

        btn_index: int = track.raw.get("button_index", -1)
        if btn_index < 0:
            fallback = track.raw.get("button_num")
            if isinstance(fallback, int) and fallback > 0:
                btn_index = fallback - 1
                print(f"[_get_audio_internal] fallback btn_index={btn_index} для {track.title!r}")
                logger.warning("[_get_audio_internal] fallback btn_index=%d для %r",
                               btn_index, track.title)
            else:
                print(f"[_get_audio_internal] нет button_index для {track.title!r}")
                raise TrackNotFoundError(f"Нет button_index для трека: {track.title}")

        search_msg = await self._get_last_search_message()
        if not search_msg:
            print(f"[_get_audio_internal] не найдено сообщение с кнопками")
            raise TrackNotFoundError("Не найдено сообщение с результатами поиска")

        print(f"[_get_audio_internal] search_msg_id={search_msg.id}  нажимаем кнопку btn_index={btn_index}")
        logger.debug("[_get_audio_internal] search_msg_id=%d  btn_index=%d",
                     search_msg.id, btn_index)

        prev_id = await self._get_last_message_id()
        await search_msg.click(btn_index)
        print(f"[_get_audio_internal] click({btn_index}) отправлен  prev_id={prev_id}  ждём аудио...")

        audio_msg = await self._wait_for_audio(prev_id=prev_id, timeout=self.AUDIO_WAIT)
        if not audio_msg or not audio_msg.audio:
            print(f"[_get_audio_internal] аудио не пришло за {self.AUDIO_WAIT}s")
            raise TrackNotFoundError(f"Аудио не получено для трека: {track.title}")

        a = audio_msg.audio
        file_size = a.file_size or 0
        print(f"[_get_audio_internal] аудио получено  msg_id={audio_msg.id}  "
              f"performer={a.performer!r}  title={a.title!r}  "
              f"duration={a.duration}s  size={file_size/1024/1024:.1f}MB")
        logger.debug("[_get_audio_internal] аудио  msg_id=%d  size=%dB",
                     audio_msg.id, file_size)

        # ── Проверка размера ────────────────────────────────────────────────
        if file_size > MAX_FILE_SIZE:
            print(f"[_get_audio_internal] файл {file_size//1024//1024}MB > 50MB — пропускаем")
            raise TrackNotFoundError(
                f"Файл слишком большой: {file_size // 1024 // 1024}MB (лимит 50MB)"
            )

        # ── Скачиваем в RAM (BytesIO) ───────────────────────────────────────
        if self._relay_bot_id and target_chat_id:
            print(f"[_get_audio_internal] скачиваем в RAM...")
            buf = await self._client.download_media(audio_msg, in_memory=True)
            # Pyrogram возвращает BytesIO
            if not isinstance(buf, io.BytesIO):
                buf = io.BytesIO(bytes(buf))
            buf.seek(0)
            size_downloaded = buf.getbuffer().nbytes
            print(f"[_get_audio_internal] скачано {size_downloaded/1024/1024:.1f}MB в RAM")
            logger.info("[_get_audio_internal] скачано %dB в RAM", size_downloaded)

            # ── Отправляем главному боту с пометкой target_chat_id ──────────
            caption = f"target:{target_chat_id}"
            file_name = f"{track.artist} - {track.title}.mp3".replace("/", "_")
            print(f"[_get_audio_internal] отправляем боту (id={self._relay_bot_id})  "
                  f"caption={caption!r}  file_name={file_name!r}")
            logger.info("[_get_audio_internal] отправка боту  caption=%r", caption)

            await self._client.send_audio(
                chat_id=self._relay_bot_id,
                audio=buf,
                caption=caption,
                file_name=file_name,
                performer=a.performer or track.artist,
                title=a.title or track.title,
                duration=a.duration or track.duration,
            )
            print(f"[_get_audio_internal] аудио отправлено боту  already_sent=True")

            # relay.py перехватит и отправит пользователю
            return AudioFile(
                telegram_file_id=a.file_id,
                telegram_unique_id=a.file_unique_id,
                title=a.title or track.title,
                artist=a.performer or track.artist,
                duration=a.duration or track.duration,
                size=size_downloaded,
                already_sent=True,
            )

        # Fallback: relay не настроен — возвращаем file_id как есть
        print(f"[_get_audio_internal] relay_bot_id не задан, возвращаем file_id напрямую")
        return AudioFile(
            telegram_file_id=a.file_id,
            telegram_unique_id=a.file_unique_id,
            title=a.title or track.title,
            artist=a.performer or track.artist,
            duration=a.duration or track.duration,
            size=file_size,
            already_sent=False,
        )

    # ── Health Check ──────────────────────────────────────────────────────────

    async def health_check(self) -> bool:
        try:
            chat = await self._client.get_chat(self.bot_username)
            ok = chat is not None
            print(f"[health_check] @{self.bot_username} → {'OK' if ok else 'FAIL'}")
            return ok
        except Exception as e:
            print(f"[health_check] @{self.bot_username} → FAIL: {e}")
            logger.warning("[health_check] @%s → FAIL: %s", self.bot_username, e)
            return False

    # ── Навигация по страницам ────────────────────────────────────────────────

    async def _navigate_to_page(self, msg: Message, target_page: int) -> Message:
        current = msg
        for step in range(target_page - 1):
            next_btn_index = _find_button_flat_index(current, "\u27a1\ufe0f")
            if next_btn_index < 0:
                print(f"[navigate] кнопка ➡️ не найдена на шаге {step+1}")
                break

            prev_id = await self._get_last_message_id()
            await current.click(next_btn_index)
            print(f"[navigate] шаг {step+1}/{target_page-1}  click({next_btn_index})")

            updated = await self._wait_for_reply(
                prev_id=prev_id, has_markup=True, timeout=self.SEARCH_WAIT
            )
            if updated:
                current = updated
            else:
                print(f"[navigate] таймаут на шаге {step+1}")
                break

        print(f"[navigate] итог  msg_id={current.id}")
        return current

    # ── Вспомогательные методы ────────────────────────────────────────────────

    async def _get_last_message_id(self) -> int:
        async for m in self._client.get_chat_history(self.bot_username, limit=1):
            return m.id
        return 0

    async def _wait_for_reply(
        self,
        prev_id: int,
        has_markup: bool = False,
        timeout: float = 12.0,
    ) -> Message | None:
        deadline = time.monotonic() + timeout
        last_seen_id = prev_id
        poll = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll += 1
            async for m in self._client.get_chat_history(self.bot_username, limit=3):
                if m.id <= last_seen_id:
                    break
                if has_markup and not m.reply_markup:
                    last_seen_id = m.id
                    continue
                print(f"[wait_reply] нашли msg_id={m.id}  poll#{poll}")
                return m

        print(f"[wait_reply] таймаут {timeout}s  poll={poll}")
        return None

    async def _wait_for_audio(self, prev_id: int, timeout: float = 20.0) -> Message | None:
        deadline = time.monotonic() + timeout
        poll = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll += 1
            async for m in self._client.get_chat_history(self.bot_username, limit=5):
                if m.id > prev_id and m.audio:
                    print(f"[wait_audio] аудио найдено  msg_id={m.id}  poll#{poll}  "
                          f"performer={m.audio.performer!r}  title={m.audio.title!r}")
                    return m

        print(f"[wait_audio] таймаут {timeout}s  poll={poll}")
        return None

    async def _get_last_search_message(self) -> Message | None:
        async for m in self._client.get_chat_history(self.bot_username, limit=5):
            if m.reply_markup:
                return m
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _find_button_flat_index(msg: Message, text: str) -> int:
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
    return hashlib.md5(query.strip().lower().encode()).hexdigest()
