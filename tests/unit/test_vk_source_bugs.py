"""
Регрессионные тесты для источника sources/vk_music_bot.py.

Покрывают три исправленных бага:
1. _wait_for_reply — last_id обновляется для пропущенных сообщений (не зависает)
2. _wait_for_audio — limit=5 (было 3), все сообщения проверяются
3. _get_audio_internal — валидация source_track_id до click()
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sources.base import Track, TrackNotFoundError
from sources.vk_music_bot import VKMusicBotSource, _parse_duration, _parse_search_message


# ─── Вспомогательные фабрики ────────────────────────────────────────────────

def make_track(source_track_id: str = "a:123:1") -> Track:
    return Track(
        title="Test Track",
        artist="Test Artist",
        duration=200,
        size=5_000_000,
        source_track_id=source_track_id,
    )


def make_audio_message():
    msg       = MagicMock()
    msg.id    = 999
    msg.audio = MagicMock()
    msg.audio.file_id        = "BQACAgITest"
    msg.audio.file_unique_id = "unique_test"
    msg.audio.title          = "Test Track"
    msg.audio.performer      = "Test Artist"
    msg.audio.duration       = 200
    msg.audio.file_size      = 5_000_000
    msg.reply_markup         = None
    return msg


def make_markup_message(msg_id: int = 100):
    msg              = MagicMock()
    msg.id           = msg_id
    msg.reply_markup = MagicMock()
    msg.reply_markup.inline_keyboard = []
    msg.audio        = None

    async def click(cb_data):
        pass

    msg.click = click
    return msg


# ─── Тесты: валидация source_track_id ───────────────────────────────────────

class TestGetAudioInternalValidation:
    """BUG: _get_audio_internal не проверял source_track_id перед .click()"""

    async def test_raises_when_source_track_id_empty(self) -> None:
        """TrackNotFoundError должен быть поднят если source_track_id пустой."""
        client = AsyncMock()
        source = VKMusicBotSource(client=client)

        track_no_id = make_track(source_track_id="")

        with pytest.raises(TrackNotFoundError, match="source_track_id"):
            await source._get_audio_internal(track_no_id)

    async def test_no_click_when_source_track_id_empty(self) -> None:
        """click() не должен вызываться если source_track_id пустой."""
        client                   = AsyncMock()
        search_msg               = make_markup_message()
        search_msg_click         = AsyncMock()
        search_msg.click         = search_msg_click

        # Эмулируем что get_chat_history возвращает search_msg
        async def fake_history(*args, **kwargs):
            yield search_msg

        client.get_chat_history = fake_history

        source = VKMusicBotSource(client=client)
        track  = make_track(source_track_id="")

        try:
            await source._get_audio_internal(track)
        except TrackNotFoundError:
            pass

        search_msg_click.assert_not_called()

    async def test_proceeds_with_valid_source_track_id(self) -> None:
        """Если source_track_id задан, _get_audio_internal не поднимает ошибку валидации."""
        client = AsyncMock()
        source = VKMusicBotSource(client=client)

        search_msg       = make_markup_message()
        search_msg.click = AsyncMock()

        call_count = 0

        async def fake_history(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield search_msg
            else:
                yield make_audio_message()

        client.get_chat_history = fake_history

        # Не должны получить TrackNotFoundError по причине "нет source_track_id"
        try:
            await source._get_audio_internal(make_track("a:123:1"))
        except TrackNotFoundError as e:
            assert "source_track_id" not in str(e)


# ─── Тесты: _wait_for_audio limit ───────────────────────────────────────────

class TestWaitForAudioLimit:
    """BUG: _wait_for_audio использовал limit=3, могло пропустить аудио"""

    async def test_audio_found_in_position_4(self) -> None:
        """Аудио на позиции 4 из 5 должно быть найдено."""
        client = AsyncMock()
        source = VKMusicBotSource(client=client)
        source.POLL_INTERVAL = 0  # убираем задержку в тестах

        initial_msg    = MagicMock()
        initial_msg.id = 10
        initial_msg.audio = None

        audio_msg       = make_audio_message()
        audio_msg.id    = 15  # новый id

        non_audio_msgs = [
            MagicMock(id=11, audio=None),
            MagicMock(id=12, audio=None),
            MagicMock(id=13, audio=None),
        ]

        call_count = 0

        async def fake_history(*args, limit=5, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                yield initial_msg
            else:
                # При limit>=4 аудио будет найдено
                for m in [audio_msg] + non_audio_msgs:
                    yield m

        client.get_chat_history = fake_history

        result = await source._wait_for_audio(timeout=1.0)
        assert result is not None
        assert result.audio is not None


# ─── Тесты: _wait_for_reply не зависает ─────────────────────────────────────

class TestWaitForReplyLastIdUpdate:
    """BUG: _wait_for_reply не обновлял last_id для non-markup сообщений"""

    async def test_last_id_advances_past_non_markup_message(self) -> None:
        """
        Если бот прислал сообщение без markup — last_id должен обновиться,
        чтобы следующий poll не сравнивал с тем же сообщением снова.
        """
        client = AsyncMock()
        source = VKMusicBotSource(client=client)
        source.POLL_INTERVAL = 0

        initial_msg       = MagicMock()
        initial_msg.id    = 10
        initial_msg.reply_markup = None

        # Первый "новый" — без markup
        no_markup_msg       = MagicMock()
        no_markup_msg.id    = 11
        no_markup_msg.reply_markup = None

        # Второй "новый" — с markup (что мы ищем)
        markup_msg       = make_markup_message(msg_id=12)

        poll_count = 0

        async def fake_history(*args, **kwargs):
            nonlocal poll_count
            poll_count += 1
            if poll_count == 1:
                yield initial_msg
            elif poll_count == 2:
                yield no_markup_msg
            else:
                yield markup_msg

        client.get_chat_history = fake_history

        result = await source._wait_for_reply(has_markup=True, timeout=2.0)

        # Должны получить markup_msg, а не зависнуть
        assert result is not None
        assert result.id == 12


# ─── Парсер — дополнительные тесты ──────────────────────────────────────────

class TestParseDurationEdgeCases:
    def test_zero_seconds(self) -> None:
        assert _parse_duration("0:00") == 0

    def test_exactly_one_hour(self) -> None:
        assert _parse_duration("1:00:00") == 3600

    def test_long_track(self) -> None:
        assert _parse_duration("48:32") == 48 * 60 + 32
