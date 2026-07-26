"""
Тесты inline хэндлера.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from bot.handlers.inline import (
    _build_results,
    _detect_language,
    _format_duration,
)
from infrastructure.database.models import Language
from sources.base import Track


def make_track(
    title: str = "Test",
    artist: str = "Artist",
    file_id: str = "FILEID",
    unique_id: str = "UNIQUE",
) -> Track:
    return Track(
        title              = title,
        artist             = artist,
        duration           = 200,
        size               = 5_000_000,
        source_track_id    = "a:1:1",
        telegram_file_id   = file_id,
        telegram_unique_id = unique_id,
    )


class TestDetectLanguage:
    def test_russian(self) -> None:
        assert _detect_language("ru") == Language.RU
        assert _detect_language("ru-RU") == Language.RU

    def test_uzbek(self) -> None:
        assert _detect_language("uz") == Language.UZ

    def test_english(self) -> None:
        assert _detect_language("en") == Language.EN
        assert _detect_language("en-US") == Language.EN

    def test_none_returns_ru(self) -> None:
        assert _detect_language(None) == Language.RU

    def test_unknown_returns_en(self) -> None:
        assert _detect_language("de") == Language.EN
        assert _detect_language("fr") == Language.EN


class TestFormatDuration:
    def test_minutes_seconds(self) -> None:
        assert _format_duration(326) == "5:26"

    def test_hours(self) -> None:
        assert _format_duration(3750) == "1:02:30"

    def test_zero(self) -> None:
        assert _format_duration(0) == "0:00"

    def test_none(self) -> None:
        assert _format_duration(None) == "0:00"


class TestBuildResults:
    def test_with_file_id(self) -> None:
        tracks  = [make_track(file_id="REAL_FILE_ID", unique_id="uniq1")]
        results = _build_results(tracks, Language.RU)

        assert len(results) == 1
        assert results[0].audio_file_id == "REAL_FILE_ID"

    def test_without_file_id_skipped(self) -> None:
        tracks = [make_track(file_id="")]
        tracks[0].telegram_file_id = None  # type: ignore[assignment]

        results = _build_results(tracks, Language.RU)
        assert len(results) == 0

    def test_max_20_results(self) -> None:
        tracks = [
            make_track(
                title=f"Track {i}",
                file_id=f"FID_{i}",
                unique_id=f"UID_{i}",
            )
            for i in range(30)
        ]
        results = _build_results(tracks, Language.RU)
        assert len(results) == 20

    def test_result_title_format(self) -> None:
        tracks  = [make_track(title="Lose Yourself", artist="Eminem")]
        results = _build_results(tracks, Language.RU)

        assert "Eminem" in results[0].title
        assert "Lose Yourself" in results[0].title
