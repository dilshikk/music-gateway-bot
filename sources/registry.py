import random

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

    def sync_enabled(self, name: str, enabled: bool) -> bool:
        """
        Синхронизирует in-memory флаг enabled после изменения в БД.
        Возвращает True если источник найден в реестре, False иначе.
        """
        source = self._sources.get(name)
        if source is not None:
            source.enabled = enabled
            return True
        return False

    def get_available(self) -> list[MusicSource]:
        """
        Активные источники с рандомизацией внутри групп одинакового приоритета.

        Алгоритм:
        1. Фильтруем только enabled=True
        2. Группируем по приоритету
        3. Внутри каждой группы — случайный порядок (чтобы нагрузка распределялась)
        4. Группы идут от высшего приоритета к низшему
        """
        active = [s for s in self._sources.values() if s.enabled]
        if not active:
            return []

        # Группировка по приоритету
        priority_groups: dict[int, list[MusicSource]] = {}
        for source in active:
            priority_groups.setdefault(source.priority, []).append(source)

        # Сортируем приоритеты по убыванию, внутри каждого — перемешиваем
        result: list[MusicSource] = []
        for priority in sorted(priority_groups.keys(), reverse=True):
            group = priority_groups[priority]
            random.shuffle(group)
            result.extend(group)

        return result

    def all(self) -> list[MusicSource]:
        return list(self._sources.values())

    def __len__(self) -> int:
        return len(self._sources)

    def __repr__(self) -> str:
        names = [s.name for s in self.get_available()]
        return f"<SourceRegistry available={names}>"
