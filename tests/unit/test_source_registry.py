"""
Тесты SourceRegistry и базового класса MusicSource.
"""
import pytest
from unittest.mock import AsyncMock

from sources.base import AudioFile, MusicSource, SearchResult, Track
from sources.registry import SourceRegistry


class _FakeSource(MusicSource):
    """Минимальная реализация для тестов."""
    name         = "Fake Source"
    bot_username = "fake_bot"
    source_type  = "telegram_bot"

    async def search(self, query: str, page: int = 1) -> SearchResult:
        return SearchResult(tracks=[], total=0, query=query)

    async def get_audio(self, track: Track) -> AudioFile:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True


class _BrokenSource(_FakeSource):
    name         = "Broken Source"
    bot_username = "broken_bot"

    async def health_check(self) -> bool:
        return False


class TestSourceRegistry:
    def test_register_and_get(self) -> None:
        registry = SourceRegistry()
        source   = _FakeSource(priority=5)
        registry.register(source)

        assert registry.get("Fake Source") is source

    def test_get_nonexistent_returns_none(self) -> None:
        registry = SourceRegistry()
        assert registry.get("Not Exists") is None

    def test_unregister(self) -> None:
        registry = SourceRegistry()
        registry.register(_FakeSource())
        registry.unregister("Fake Source")
        assert registry.get("Fake Source") is None

    def test_get_available_only_enabled(self) -> None:
        registry = SourceRegistry()
        enabled  = _FakeSource(priority=10, enabled=True)
        disabled = _BrokenSource(enabled=False)
        registry.register(enabled)
        registry.register(disabled)

        available = registry.get_available()
        assert len(available) == 1
        assert available[0].name == "Fake Source"

    def test_get_available_sorted_by_priority(self) -> None:
        registry = SourceRegistry()

        low  = _FakeSource(priority=1)
        high = _BrokenSource(priority=10, enabled=True)

        # Патчим имена чтобы не пересекались
        low.name  = "Low Priority"
        high.name = "High Priority"

        registry.register(low)
        registry.register(high)

        available = registry.get_available()
        assert available[0].name == "High Priority"
        assert available[1].name == "Low Priority"

    def test_len(self) -> None:
        registry = SourceRegistry()
        assert len(registry) == 0
        registry.register(_FakeSource())
        assert len(registry) == 1


class TestMusicSourceStats:
    def test_record_success(self) -> None:
        source = _FakeSource()
        source.record_success(100.0)
        source.record_success(200.0)

        assert source._success_count == 2
        assert source.avg_response_ms == 150.0

    def test_record_error(self) -> None:
        source = _FakeSource()
        source.record_success(100.0)
        source.record_error()

        assert source.error_rate == pytest.approx(0.5)

    def test_avg_response_zero_when_no_success(self) -> None:
        source = _FakeSource()
        assert source.avg_response_ms == 0.0

    def test_error_rate_zero_when_no_calls(self) -> None:
        source = _FakeSource()
        assert source.error_rate == 0.0
