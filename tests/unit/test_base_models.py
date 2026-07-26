"""
Тесты для sources/base.py — модели данных и исключения.
"""
from datetime import datetime, timezone

import pytest

from sources.base import (
    AudioFile,
    MusicSource,
    SearchResult,
    SourceFloodWaitError,
    Track,
)


class TestSearchResultFetchedAt:
    """
    BUG FIX: SearchResult.fetched_at ранее использовал datetime.utcnow(),
    который устарел в Python 3.12+ и возвращает naive datetime.
    Теперь должен использоваться datetime.now(timezone.utc) — aware datetime.
    """

    def test_fetched_at_is_timezone_aware(self) -> None:
        result = SearchResult(tracks=[], total=0, query="test")
        assert result.fetched_at.tzinfo is not None, (
            "fetched_at должен быть timezone-aware (UTC), "
            "не naive datetime (datetime.utcnow() устарел)"
        )

    def test_fetched_at_is_utc(self) -> None:
        result = SearchResult(tracks=[], total=0, query="test")
        assert result.fetched_at.tzinfo == timezone.utc

    def test_two_results_have_different_timestamps(self) -> None:
        import time
        r1 = SearchResult(tracks=[], total=0, query="a")
        time.sleep(0.01)
        r2 = SearchResult(tracks=[], total=0, query="b")
        assert r2.fetched_at >= r1.fetched_at

    def test_fetched_at_can_be_overridden(self) -> None:
        custom_ts = datetime(2024, 1, 1, tzinfo=timezone.utc)
        result = SearchResult(tracks=[], total=0, query="test", fetched_at=custom_ts)
        assert result.fetched_at == custom_ts


class TestTrack:
    def test_defaults(self) -> None:
        t = Track(title="Song", duration=200, size=5_000_000, source_track_id="a:1:1")
        assert t.artist == ""
        assert t.bitrate == 0
        assert t.is_lossless is False
        assert t.thumbnail_url == ""
        assert t.raw == {}

    def test_with_artist(self) -> None:
        t = Track(
            title="Lose Yourself",
            artist="Eminem",
            duration=326,
            size=12_400_000,
            source_track_id="a:111:1",
            bitrate=320,
        )
        assert t.artist == "Eminem"
        assert t.bitrate == 320


class TestSourceFloodWaitError:
    def test_carries_seconds(self) -> None:
        err = SourceFloodWaitError(seconds=60)
        assert err.seconds == 60

    def test_str_representation(self) -> None:
        err = SourceFloodWaitError(seconds=30)
        assert "30" in str(err)
