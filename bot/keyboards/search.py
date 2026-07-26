from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sources.base import SearchResult, Track


def build_search_results_keyboard(
    result: SearchResult,
    task_id: str,
) -> InlineKeyboardMarkup:
    """Клавиатура со списком треков + навигация."""
    builder = InlineKeyboardBuilder()

    for i, track in enumerate(result.tracks):
        duration = _format_duration(track.duration)
        size_mb  = round(track.size / 1024 / 1024, 1)
        label    = f"{i + 1}. {track.artist} — {track.title}"
        if len(label) > 48:
            label = label[:45] + "..."
        sub = f"{duration} • {size_mb}M • {track.bitrate}k"

        builder.row(
            InlineKeyboardButton(
                text=f"{label}\n{sub}",
                callback_data=f"dl:{task_id}:{i}",
            )
        )

    # Навигация
    nav_row = []
    if result.page > 1:
        nav_row.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"page:{task_id}:{result.page - 1}")
        )
    nav_row.append(
        InlineKeyboardButton(
            text=f"{result.page} / {max(1, -(-result.total // 8))}",
            callback_data="noop",
        )
    )
    if result.has_next:
        nav_row.append(
            InlineKeyboardButton(text="➡️", callback_data=f"page:{task_id}:{result.page + 1}")
        )
    builder.row(*nav_row)
    builder.row(
        InlineKeyboardButton(text="❌ Закрыть", callback_data="close")
    )

    return builder.as_markup()


def build_downloading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⏳ Скачиваю...", callback_data="noop")
    ]])


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
