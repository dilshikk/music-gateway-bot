"""
Inline-режим бота.
Фикс БАГ 2: единый chosen_inline_result хэндлер.
Фикс БАГ 8: стабильный result id через md5.
"""
import asyncio
import hashlib
import logging

from aiogram import Router
from aiogram.types import (
    ChosenInlineResult,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InputTextMessageContent,
)

from core.cache_manager import CacheManager
from core.search_manager import SearchContext, SearchManager
from infrastructure.database.models import Language
from infrastructure.database.repositories.user_repo import UserRepository
from infrastructure.database.session import async_session_factory
from infrastructure.i18n.translator import t
from sources.base import SourceUnavailableError, Track

logger = logging.getLogger(__name__)

router = Router(name="inline")

RESULTS_PER_PAGE = 20
INLINE_CACHE_TTL = 60


@router.inline_query()
async def handle_inline_query(
    inline_query: InlineQuery,
    search_manager: SearchManager,
    cache: CacheManager,
) -> None:
    query  = inline_query.query.strip()
    offset = inline_query.offset or "1"
    tg_id  = inline_query.from_user.id
    lang   = _detect_language(inline_query.from_user.language_code)

    if len(query) < 2:
        await _answer_hint(inline_query, lang)
        return

    try:
        page = int(offset)
    except ValueError:
        page = 1

    async with async_session_factory() as session:
        repo    = UserRepository(session)
        db_user = await repo.get_by_telegram_id(tg_id)

    allowed, retry_after = await cache.check_rate_limit(
        user_id=tg_id,
        max_requests=30,
        window=60,
        key_suffix=":inline",
    )
    if not allowed:
        await _answer_rate_limit(inline_query, lang, retry_after)
        return

    try:
        ctx    = SearchContext(query=query, user_id=tg_id, page=page)
        result = await asyncio.wait_for(
            search_manager.search(ctx),
            timeout=8.0,
        )
    except asyncio.TimeoutError:
        logger.warning("[Inline] Timeout user=%d query=%r", tg_id, query)
        await _answer_error(inline_query, lang, "timeout")
        return
    except SourceUnavailableError:
        await _answer_error(inline_query, lang, "unavailable")
        return
    except Exception as e:
        logger.error("[Inline] Error user=%d: %s", tg_id, e, exc_info=True)
        await _answer_error(inline_query, lang, "unknown")
        return

    if not result.tracks:
        await _answer_not_found(inline_query, lang, query)
        return

    items       = _build_results(result.tracks, lang)
    next_offset = str(page + 1) if result.has_next else ""

    await inline_query.answer(
        results     = items,
        next_offset = next_offset,
        cache_time  = INLINE_CACHE_TTL,
        is_personal = True,
    )


# БАГ 2: единый хэндлер — inline_feedback.py не нужен
@router.chosen_inline_result()
async def handle_chosen_result(
    chosen: ChosenInlineResult,
    cache: CacheManager,
) -> None:
    tg_id = chosen.from_user.id
    query = chosen.query.strip()
    if not query:
        return
    await cache.increment_popular(query)
    await cache.add_to_history(tg_id, query)
    logger.info("[Inline] Chosen user=%d result=%r query=%r", tg_id, chosen.result_id, query)


def _build_results(tracks: list[Track], lang: Language) -> list[InlineQueryResultAudio]:
    results = []
    for track in tracks[:RESULTS_PER_PAGE]:
        if not track.telegram_file_id:
            continue

        # БАГ 8: стабильный id через md5
        stable_key = track.telegram_unique_id or track.source_track_id or ""
        result_id  = "t:" + hashlib.md5(stable_key.encode()).hexdigest()[:16]

        artist  = track.artist or "Unknown"
        title   = track.title  or "Unknown"
        caption = t(lang, "download-caption", artist=artist, title=title)

        results.append(InlineQueryResultAudio(
            id            = result_id,
            audio_file_id = track.telegram_file_id,
            title         = f"{artist} — {title}",
            caption       = caption,
        ))
    return results


def _detect_language(language_code: str | None) -> Language:
    if not language_code:
        return Language.RU
    code = language_code.lower()
    if code.startswith("ru"): return Language.RU
    if code.startswith("uz"): return Language.UZ
    if code.startswith("en"): return Language.EN
    return Language.EN


def _format_duration(seconds: int | None) -> str:
    if not seconds:
        return "0:00"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _answer_hint(inline_query: InlineQuery, lang: Language) -> None:
    hints = {
        Language.RU: ("🔍 Введите название трека", "Например: eminem lose yourself"),
        Language.UZ: ("🔍 Trek nomini kiriting", "Misol: eminem lose yourself"),
        Language.EN: ("🔍 Type a track name", "Example: eminem lose yourself"),
    }
    title, body = hints.get(lang, hints[Language.EN])
    await inline_query.answer(
        results=[InlineQueryResultArticle(
            id="hint", title=title, description=body,
            input_message_content=InputTextMessageContent(message_text=body),
        )],
        cache_time=300, is_personal=False,
    )


async def _answer_not_found(inline_query: InlineQuery, lang: Language, query: str) -> None:
    text = t(lang, "search-not-found", query=query)
    await inline_query.answer(
        results=[InlineQueryResultArticle(
            id="not_found", title=text,
            input_message_content=InputTextMessageContent(message_text=text),
        )],
        cache_time=5,
    )


async def _answer_rate_limit(inline_query: InlineQuery, lang: Language, retry_after: int) -> None:
    text = t(lang, "rate-limit-minute", seconds=retry_after)
    await inline_query.answer(
        results=[InlineQueryResultArticle(
            id="rate_limit", title=f"⏳ {text}",
            input_message_content=InputTextMessageContent(message_text=text),
        )],
        cache_time=5,
    )


async def _answer_error(inline_query: InlineQuery, lang: Language, reason: str) -> None:
    keys = {"timeout": "search-timeout", "unavailable": "search-error", "unknown": "search-error"}
    text = t(lang, keys.get(reason, "search-error"))
    await inline_query.answer(
        results=[InlineQueryResultArticle(
            id=f"error_{reason}", title=text,
            input_message_content=InputTextMessageContent(message_text=text),
        )],
        cache_time=5,
    )
