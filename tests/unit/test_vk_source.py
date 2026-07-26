"""
Тесты парсера ответов @vkmusic_bot.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from sources.vk_music_bot import (
    VKMusicBotSource,
    _parse_duration,
    _parse_search_message,
)


def make_mock_message(text: str, buttons: list[list[dict]] | None = None):
    """Создаёт минимальный мок pyrogram.Message."""
    msg = MagicMock()
    msg.text = text
    msg.reply_markup = None

    if buttons:
        msg.reply_markup = MagicMock()
        rows = []
        for row in buttons:
            btn_row = []
            for btn in row:
                b = MagicMock()
                b.text          = btn["text"]
                b.callback_data = btn["callback_data"]
                btn_row.append(b)
            rows.append(btn_row)
        msg.reply_markup.inline_keyboard = rows

    return msg


SAMPLE_TEXT = """\
🔍 Eminem
Результаты 1-8 из 500

1. Eminem - Lose Yourself 5:26 12.4M 320k
2. Eminem - Stan 6:44 15.5M 320k
3. Eminem - Without Me 4:50 11.1M 320k
4. Eminem - Rap God 6:04 13.9M 320k
"""

SAMPLE_BUTTONS = [
    [
        {"text": "1", "callback_data": "a:111:1"},
        {"text": "2", "callback_data": "a:222:1"},
        {"text": "3", "callback_data": "a:333:1"},
        {"text": "4", "callback_data": "a:444:1"},
    ],
    [
        {"text": "⬅️",  "callback_data": "sb:1:abc"},
        {"text": "❌",  "callback_data": "settings:close"},
        {"text": "➡️",  "callback_data": "ss:8::0:0:1:abc"},
    ],
]


class TestParseDuration:
    def test_minutes_seconds(self) -> None:
        assert _parse_duration("5:26") == 326

    def test_hours_minutes_seconds(self) -> None:
        assert _parse_duration("1:02:30") == 3750

    def test_zero(self) -> None:
        assert _parse_duration("0:00") == 0

    def test_long_track(self) -> None:
        assert _parse_duration("48:32") == 2912


class TestParseSearchMessage:
    def test_parses_track_count(self) -> None:
        msg    = make_mock_message(SAMPLE_TEXT, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)
        assert result.total == 500

    def test_parses_tracks(self) -> None:
        msg    = make_mock_message(SAMPLE_TEXT, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)
        assert len(result.tracks) == 4

    def test_first_track_data(self) -> None:
        msg    = make_mock_message(SAMPLE_TEXT, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)
        t      = result.tracks[0]

        assert t.artist          == "Eminem"
        assert t.title           == "Lose Yourself"
        assert t.duration        == 326
        assert t.bitrate         == 320
        assert t.source_track_id == "a:111:1"

    def test_callback_data_mapped_correctly(self) -> None:
        msg    = make_mock_message(SAMPLE_TEXT, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)

        cbs = [t.source_track_id for t in result.tracks]
        assert cbs == ["a:111:1", "a:222:1", "a:333:1", "a:444:1"]

    def test_has_next_when_not_last_page(self) -> None:
        msg    = make_mock_message(SAMPLE_TEXT, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)
        assert result.has_next is True

    def test_no_next_when_last_page(self) -> None:
        text = SAMPLE_TEXT.replace("1-8 из 500", "497-500 из 500")
        msg  = make_mock_message(text, SAMPLE_BUTTONS)
        result = _parse_search_message(msg)
        # 500 треков, показаны 497-500 — это последняя страница
        # has_next = end < total → 500 < 500 = False
        assert result.has_next is False

    def test_empty_text_returns_empty_result(self) -> None:
        msg    = make_mock_message("")
        result = _parse_search_message(msg)
        assert result.tracks == []
        assert result.total  == 0
