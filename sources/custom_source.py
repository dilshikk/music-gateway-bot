"""
Шаблон для добавления нового источника музыки.
Скопируй этот файл, переименуй класс и реализуй методы.
"""
from sources.base import AudioFile, MusicSource, SearchResult, Track


class CustomMusicSource(MusicSource):
    name         = "My Custom Source"
    bot_username = "my_music_bot"
    source_type  = "telegram_bot"  # или "api" | "database"

    async def search(self, query: str, page: int = 1) -> SearchResult:
        # TODO: отправить запрос в источник и вернуть SearchResult
        raise NotImplementedError

    async def get_audio(self, track: Track) -> AudioFile:
        # TODO: получить аудиофайл и вернуть AudioFile
        raise NotImplementedError

    async def health_check(self) -> bool:
        # TODO: проверить доступность источника
        return False
