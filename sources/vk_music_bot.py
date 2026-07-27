"""
VKMusicBotSource — источник музыки через @vkmusic_bot.

Архитектура (один userbot + asyncio.Queue + служебная группа):
  ┌─────────────┐     put()      ┌──────────┐    один за раз
  │ user A dl:  │ ─────────────► │          │
  │ user B dl:  │ ─────────────► │  Queue   │ ──► _worker ──► @vkmusic_bot
  │ user C dl:  │ ─────────────► │          │
  └─────────────┘                └──────────┘
                                      │
                               получил аудио
                                      │
                               forward → служебная группа (LOG_GROUP_ID)
                               caption = "user:{target_chat_id}"
                                      │
                               relay.py: читает caption → send_audio → пользователь

Преимущества схемы с группой:
  - Userbot пересылает file_id напрямую, без скачивания в RAM
  - Бот получает родной Bot API file_id из сообщения в группе
  - Никаких временных файлов на диске
"""
import asyncio
import hashlib
import logging
import re
import time
from dataclasses import dataclass
from typing import Any

from pyrogram import Client
from pyrogram.errors import FloodWait
from pyrogram.types import Message

from config.settings import settings
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

# Разделитель артист/название: обычный дефис, en-dash (–) или em-dash (—)
_ARTIST_TITLE_SEP = re.compile(r"\s+[-\u2013\u2014]\s+")


# ─── Внутренние типы очереди ──────────────────────────────────────────────────

@dataclass
class _QueueItem:
    """Одна задача в очереди воркера."""
    coro: Any               # coroutine для выполнения
    future: asyncio.Future  # сюда кладём результат или исключение
    label: str = ""         # для отладки


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

    Каждый Track сохраняет search_chat_id, search_msg_id и parsed_at
    (UTC Unix float) — точный адрес сообщения-источника.
    _get_audio_internal использует эти данные для request_callback_answer().
    Треки без callback_data отбрасываются — они не скачаются.

    parsed_at хранится как time.time() (не monotonic), чтобы пережить
    JSON-сериализацию при кэшировании в Redis.
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

    # Собираем только callback_data — button_index больше не нужен,
    # клик идёт напрямую по callback_data через request_callback_answer().
    button_map: dict[int, str] = {}
    if msg.reply_markup:
        for row in msg.reply_markup.inline_keyboard:
            for btn in row:
                if btn.text.isdigit() and btn.callback_data:
                    button_map[int(btn.text)] = btn.callback_data
    logger.debug("[parse] кнопок с треками: %d", len(button_map))

    line_pattern = re.compile(
        r"^(\d+)\.\s+(.+?)\s+"
        r"(\d+:\d{2})\s+"
        r"([\d.]+)M\s+"
        r"(\d+)k"
        r"(\s+Lossless)?",
        re.MULTILINE,
    )

    # Общий для всех треков этой страницы адрес сообщения + UTC-время парсинга.
    # parsed_at = time.time() (не monotonic!) — выживает после JSON round-trip в Redis.
    msg_ref = {
        "search_chat_id": msg.chat.id,
        "search_msg_id": msg.id,
        "parsed_at": time.time(),
    }

    for m in line_pattern.finditer(text):
        num       = int(m.group(1))
        raw_title = m.group(2).strip()
        duration  = _parse_duration(m.group(3))
        size_mb   = float(m.group(4))
        bitrate   = int(m.group(5))
        lossless  = bool(m.group(6))

        # Разделяем артиста и название по дефису, en-dash или em-dash
        sep_match = _ARTIST_TITLE_SEP.search(raw_title)
        if sep_match:
            artist = raw_title[:sep_match.start()].strip()
            title  = raw_title[sep_match.end():].strip()
        else:
            artist, title = "", raw_title

        cbd = button_map.get(num, "")
        if not cbd:
            # Трек без кнопки скачать невозможно — не отдаём пользователю
            logger.warning("[parse] нет callback_data для трека #%d %r — пропускаем", num, raw_title)
            continue

        track_raw = {
            "button_num": num,
            "callback_data": cbd,
            **msg_ref,  # search_chat_id, search_msg_id, parsed_at
        }
        tracks.append(Track(
            title=title,
            artist=artist,
            duration=duration,
            size=int(size_mb * 1024 * 1024),
            bitrate=bitrate,
            is_lossless=lossless,
            source_track_id=cbd,
            raw=track_raw,
        ))
        # DEBUG: убедиться, что все нужные ключи присутствуют в raw
        print(f"[parse] DEBUG track={title!r} artist={artist!r} raw_keys={list(track_raw.keys())}")

    logger.debug("[parse] итог: %d треков  msg_id=%d", len(tracks), msg.id)
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
      1. request_callback_answer() по точному (chat_id, msg_id, callback_data)
      2. Дождаться аудио-сообщения
      3. Переслать в служебную группу LOG_GROUP_ID с caption="user:{chat_id}"
      4. relay.py: видит аудио в группе → читает caption → send_audio пользователю
    """

    name         = "VK Music Bot"
    bot_username = "vkmusic_bot"
    source_type  = "telegram_bot"

    SEARCH_WAIT   = 12.0
    AUDIO_WAIT    = 20.0
    POLL_INTERVAL = 0.5
    STALE_TTL     = 15 * 60  # секунды; синхронизировано с SESSION_TTL в search.py

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
        # relay_bot_id оставлен для обратной совместимости, не используется в новой схеме
        self._relay_bot_id = relay_bot_id

        # Очередь задач — сердце архитектуры
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
        Берёт задачи из очереди по одной и выполняет последовательно.
        None в очереди — сигнал остановки.
        """
        logger.info("[worker] старт  queue_size=%d", self._queue.qsize())
        print("[worker] старт")

        while True:
            item = await self._queue.get()

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
                print(f"[worker] задача {item.label!r} ошибка: {e}")
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
        print(f"[submit] задача {label!r} в очереди  позиция≈{pos}")
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
            print(f"[search] ошибка: {type(e).__name__}: {e}")
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
            print(f"[get_audio] готово  already_sent={audio.already_sent}  за {elapsed:.0f}ms")
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
        print("[_search_internal] запрос отправлен  ждём ответ с кнопками...")

        msg = await self._wait_for_new_message(prev_id=prev_id, has_markup=True, timeout=self.SEARCH_WAIT)
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
        # DEBUG: показать полное содержимое track.raw при входе
        print(f"[_get_audio_internal] DEBUG track.raw={track.raw!r}")

        if not track.source_track_id:
            print(f"[_get_audio_internal] source_track_id пустой для {track.title!r}")
            raise TrackNotFoundError(f"Трек не имеет source_track_id: {track.title}")

        search_chat_id = track.raw.get("search_chat_id")
        search_msg_id  = track.raw.get("search_msg_id")
        parsed_at      = track.raw.get("parsed_at", 0.0)

        if not search_chat_id or not search_msg_id:
            print(f"[_get_audio_internal] нет адреса сообщения для {track.title!r}")
            raise TrackNotFoundError(f"Нет ссылки на сообщение результатов: {track.title}")

        # Ранняя проверка TTL по UTC-времени (time.time(), не monotonic)
        age = time.time() - parsed_at
        if age > self.STALE_TTL:
            print(f"[_get_audio_internal] результаты устарели  age={age:.0f}s > ttl={self.STALE_TTL}s")
            raise TrackNotFoundError("Результаты поиска устарели — повторите поиск")

        print(
            f"[_get_audio_internal] fetching  chat={search_chat_id}  msg_id={search_msg_id}  "
            f"age={age:.0f}s  cbd={track.source_track_id!r}"
        )
        logger.debug("[_get_audio_internal] get_messages  chat=%s  msg_id=%d",
                     search_chat_id, search_msg_id)

        # Проверяем, что сообщение и нужная кнопка ещё существуют
        try:
            msgs = await self._client.get_messages(search_chat_id, message_ids=search_msg_id)
            search_msg: Message | None = msgs if isinstance(msgs, Message) else (msgs[0] if msgs else None)
        except Exception as e:
            print(f"[_get_audio_internal] сообщение {search_msg_id} недоступно: {e}")
            raise TrackNotFoundError("Сообщение с результатами поиска не найдено") from e

        if not search_msg or not search_msg.reply_markup:
            print(f"[_get_audio_internal] сообщение {search_msg_id} без клавиатуры")
            raise TrackNotFoundError("Результаты поиска устарели или были изменены — повторите поиск")

        if not _callback_exists(search_msg, track.source_track_id):
            print(f"[_get_audio_internal] callback_data {track.source_track_id!r} не найден в msg {search_msg_id}")
            raise TrackNotFoundError("Кнопка трека больше не найдена — результаты обновились")

        print(
            f"[_get_audio_internal] клик  chat={search_chat_id}  msg={search_msg_id}  "
            f"cbd={track.source_track_id!r}  track={track.title!r}"
        )

        prev_id = await self._get_last_message_id()
        try:
            await self._client.request_callback_answer(
                chat_id=search_chat_id,
                message_id=search_msg_id,
                callback_data=track.source_track_id,
            )
        except Exception as e:
            print(f"[_get_audio_internal] ошибка request_callback_answer: {e}")
            raise TrackNotFoundError(f"Не удалось нажать кнопку трека: {e}") from e

        print(f"[_get_audio_internal] callback отправлен  prev_id={prev_id}  ждём аудио...")

        audio_msg = await self._wait_for_audio(prev_id=prev_id, timeout=self.AUDIO_WAIT)
        if not audio_msg or not audio_msg.audio:
            print(f"[_get_audio_internal] аудио не пришло за {self.AUDIO_WAIT}s")
            raise TrackNotFoundError(f"Аудио не получено для трека: {track.title}")

        a = audio_msg.audio
        file_size = a.file_size or 0
        print(
            f"[_get_audio_internal] аудио получено  msg_id={audio_msg.id}  "
            f"performer={a.performer!r}  title={a.title!r}  "
            f"duration={a.duration}s  size={file_size / 1024 / 1024:.1f}MB"
        )
        logger.debug("[_get_audio_internal] аудио  msg_id=%d  size=%dB", audio_msg.id, file_size)

        # ── Проверка размера ────────────────────────────────────────────────
        if file_size > MAX_FILE_SIZE:
            print(f"[_get_audio_internal] файл {file_size // 1024 // 1024}MB > 50MB — отклоняем")
            raise TrackNotFoundError(
                f"Файл слишком большой: {file_size // 1024 // 1024}MB (лимит 50MB)"
            )

        # ── Схема с группой: пересылаем в LOG_GROUP_ID ──────────────────────
        log_group_id = settings.LOG_GROUP_ID
        if log_group_id != 0 and target_chat_id:
            caption = f"user:{target_chat_id}"
            print(
                f"[_get_audio_internal] пересылаем в группу {log_group_id}  "
                f"caption={caption!r}  file_id={a.file_id!r}"
            )
            logger.info(
                "[_get_audio_internal] forward → group=%d  caption=%r",
                log_group_id, caption,
            )

            # Pyrogram хранит пиры в локальном SQLite-кэше.
            # get_chat() принудительно резолвит и кэширует группу перед отправкой.
            try:
                await self._client.get_chat(log_group_id)
                print(f"[_get_audio_internal] peer {log_group_id} резолвлен успешно")
            except Exception as resolve_err:
                print(
                    f"[_get_audio_internal] ОШИБКА резолва группы {log_group_id}: {resolve_err}\n"
                    f"  → Убедитесь: userbot вступил в группу и отправил хотя бы одно сообщение!"
                )
                logger.error(
                    "[_get_audio_internal] не удалось резолвить group=%d: %s",
                    log_group_id, resolve_err,
                )
                raise TrackNotFoundError(
                    f"Userbot не может найти группу {log_group_id}. "
                    f"Проверьте: userbot вступил в группу? "
                    f"Отправьте любое сообщение в группу с аккаунта userbot."
                ) from resolve_err

            await self._client.send_audio(
                chat_id=log_group_id,
                audio=a.file_id,
                caption=caption,
                performer=a.performer or track.artist,
                title=a.title or track.title,
                duration=a.duration or track.duration,
            )
            print(f"[_get_audio_internal] переслано в группу  already_sent=True")

            return AudioFile(
                telegram_file_id=a.file_id,
                telegram_unique_id=a.file_unique_id,
                title=a.title or track.title,
                artist=a.performer or track.artist,
                duration=a.duration or track.duration,
                size=file_size,
                already_sent=True,
            )

        # ── Fallback: группа не настроена ────────────────────────────────────
        print("[_get_audio_internal] LOG_GROUP_ID=0 — возвращаем file_id напрямую (fallback)")
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
        """
        Листает страницы @vkmusic_bot нажимая ➡️.

        ВАЖНО: @vkmusic_bot редактирует существующее сообщение при пагинации,
        не присылает новое. Поэтому после клика поллим get_messages(msg_id)
        пока строка "Результаты X-Y из Z" не изменится.

        _find_button_flat_index для кнопки ➡️ безопасен: клик происходит
        немедленно внутри воркера, без разрыва во времени.
        """
        current = msg
        for step in range(target_page - 1):
            next_btn_index = _find_button_flat_index(current, "\u27a1\ufe0f")
            if next_btn_index < 0:
                print(f"[navigate] кнопка ➡️ не найдена на шаге {step + 1}")
                break

            # Запоминаем текущий текст «Результаты X-Y из Z» как маркер изменения
            old_text = current.text or ""
            old_results_line = _extract_results_line(old_text)
            print(
                f"[navigate] шаг {step + 1}/{target_page - 1}  "
                f"click({next_btn_index})  old_results={old_results_line!r}"
            )

            await current.click(next_btn_index)

            # Ждём пока сообщение обновится (бот редактирует, не присылает новое)
            updated = await self._wait_for_message_edit(
                msg_id=current.id,
                old_results_line=old_results_line,
                timeout=self.SEARCH_WAIT,
            )
            if updated:
                current = updated
                print(f"[navigate] страница обновилась  msg_id={current.id}  "
                      f"new_results={_extract_results_line(current.text or '')!r}")
            else:
                print(f"[navigate] таймаут на шаге {step + 1} — остаёмся на текущей странице")
                break

        print(f"[navigate] итог  msg_id={current.id}")
        return current

    # ── Вспомогательные методы ────────────────────────────────────────────────

    async def _get_last_message_id(self) -> int:
        async for m in self._client.get_chat_history(self.bot_username, limit=1):
            return m.id
        return 0

    async def _wait_for_new_message(
        self,
        prev_id: int,
        has_markup: bool = False,
        timeout: float = 12.0,
    ) -> Message | None:
        """Ждёт новое сообщение с id > prev_id. Используется при первом поиске."""
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
                print(f"[wait_new_msg] нашли msg_id={m.id}  poll#{poll}")
                return m

        print(f"[wait_new_msg] таймаут {timeout}s  poll={poll}")
        return None

    async def _wait_for_message_edit(
        self,
        msg_id: int,
        old_results_line: str,
        timeout: float = 12.0,
    ) -> Message | None:
        """
        Ждёт пока @vkmusic_bot отредактирует сообщение msg_id.
        Сравниваем строку «Результаты X-Y из Z» — она меняется при каждом листании.
        """
        deadline = time.monotonic() + timeout
        poll = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll += 1
            msgs = await self._client.get_messages(self.bot_username, message_ids=msg_id)
            m: Message | None = msgs if isinstance(msgs, Message) else (msgs[0] if msgs else None)
            if not m:
                continue
            new_results_line = _extract_results_line(m.text or "")
            if new_results_line and new_results_line != old_results_line:
                print(f"[wait_edit] изменение обнаружено  poll#{poll}  "
                      f"{old_results_line!r} → {new_results_line!r}")
                return m

        print(f"[wait_edit] таймаут {timeout}s  poll={poll}  msg_id={msg_id}")
        return None

    async def _wait_for_audio(self, prev_id: int, timeout: float = 20.0) -> Message | None:
        deadline = time.monotonic() + timeout
        poll = 0

        while time.monotonic() < deadline:
            await asyncio.sleep(self.POLL_INTERVAL)
            poll += 1
            async for m in self._client.get_chat_history(self.bot_username, limit=5):
                if m.id > prev_id and m.audio:
                    print(
                        f"[wait_audio] аудио найдено  msg_id={m.id}  poll#{poll}  "
                        f"performer={m.audio.performer!r}  title={m.audio.title!r}"
                    )
                    return m

        print(f"[wait_audio] таймаут {timeout}s  poll={poll}")
        return None


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _find_button_flat_index(msg: Message, text: str) -> int:
    """Находит плоский индекс кнопки по тексту. Используется только для ➡️ в пагинации."""
    if not msg.reply_markup:
        return -1
    idx = 0
    for row in msg.reply_markup.inline_keyboard:
        for btn in row:
            if btn.text == text:
                return idx
            idx += 1
    return -1


def _callback_exists(msg: Message, callback_data: str) -> bool:
    """Проверяет, есть ли кнопка с данным callback_data в клавиатуре сообщения."""
    if not msg.reply_markup:
        return False
    for row in msg.reply_markup.inline_keyboard:
        for btn in row:
            if btn.callback_data == callback_data:
                return True
    return False


def _extract_results_line(text: str) -> str:
    """Извлекает строку 'Результаты X-Y из Z' для сравнения при пагинации."""
    m = re.search(r"Результаты\s+\d+-\d+\s+из\s+\d+", text)
    return m.group(0) if m else ""


def _make_query_hash(query: str) -> str:
    return hashlib.md5(query.strip().lower().encode()).hexdigest()
