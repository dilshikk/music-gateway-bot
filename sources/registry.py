from sources.base import MusicSource


class SourceRegistry:
    """
    Реестр всех источников музыки.
    Search Manager обращается только к нему — не к источникам напрямую.
    """

    def __init__(self) -> None:
        self._sources: dict[str, MusicSource] = {}

    def register(self, source: MusicSource) -> None:
        """Зарегистрировать источник."""
        self._sources[source.name] = source

    def unregister(self, name: str) -> None:
        self._sources.pop(name, None)

    def get(self, name: str) -> MusicSource | None:
        return self._sources.get(name)

    def get_available(self) -> list[MusicSource]:
        """Активные источники, отсортированные по приоритету (выше = лучше)."""
        return sorted(
            [s for s in self._sources.values() if s.enabled],
            key=lambda s: s.priority,
            reverse=True,
        )

    def all(self) -> list[MusicSource]:
        return list(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __repr__(self) -> str:
        names = [s.name for s in self.get_available()]
        return f"<SourceRegistry sources={names}>"
