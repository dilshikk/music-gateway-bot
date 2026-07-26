"""
Factory Boy фабрики для создания тестовых данных.
"""
import factory
from factory import Faker

from infrastructure.database.models import (
    AdminRole,
    Language,
    Search,
    SearchStatus,
    Source,
    Track,
    User,
    Userbot,
    UserbotStatus,
)


class UserFactory(factory.Factory):
    class Meta:
        model = User

    telegram_id    = factory.Sequence(lambda n: 100000 + n)
    username       = Faker("user_name")
    first_name     = Faker("first_name")
    language       = Language.RU
    premium        = False
    daily_requests = 0
    total_requests = 0
    is_banned      = False
    ban_reason     = None


class UserbotFactory(factory.Factory):
    class Meta:
        model = Userbot

    phone          = factory.Sequence(lambda n: f"+7999{n:07d}")
    api_id         = factory.Sequence(lambda n: 1000000 + n)
    api_hash       = Faker("md5")
    session_string = Faker("sha256")
    status         = UserbotStatus.IDLE
    weight         = 1
    daily_limit    = 200
    requests_today = 0
    requests_total = 0
    error_count    = 0


class SourceFactory(factory.Factory):
    class Meta:
        model = Source

    name           = factory.Sequence(lambda n: f"Source {n}")
    bot_username   = factory.Sequence(lambda n: f"source_bot_{n}")
    type           = "telegram_bot"
    priority       = 1
    enabled        = True
    timeout        = 30
    success_count  = 0
    error_count    = 0
    avg_response_ms = 0.0


class TrackFactory(factory.Factory):
    class Meta:
        model = Track

    title               = Faker("sentence", nb_words=3)
    artist              = Faker("name")
    duration            = factory.Faker("pyint", min_value=60, max_value=600)
    size                = factory.Faker("pyint", min_value=1_000_000, max_value=50_000_000)
    telegram_file_id    = factory.Sequence(lambda n: f"file_id_{n}")
    telegram_unique_id  = factory.Sequence(lambda n: f"unique_{n}")
    play_count          = 0


class SearchFactory(factory.Factory):
    class Meta:
        model = Search

    query      = Faker("sentence", nb_words=2)
    query_hash = Faker("md5")
    status     = SearchStatus.DONE
    duration_ms = factory.Faker("pyint", min_value=100, max_value=5000)
