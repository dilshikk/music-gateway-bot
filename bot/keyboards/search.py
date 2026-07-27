from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from sources.base import SearchResult, Track


def build_search_results_keyboard(
    result: SearchResult,
    task_id: str,
) -> InlineKeyboardMarkup:
    """
    Клавиатура в стиле @vkmusic_bot:
      - Ряды по 4 кнопки с номерами треков
      - Навигация: ⬅️  страница/всего  ➡️
      - Закрыть ❌
    Треки выводятся в тексте сообщения, а не в кнопках.
    """
    builder = InlineKeyboardBuilder()

    # Кнопки с номерами треков, по 4 в ряд
    buttons = [
        InlineKeyboardButton(
            text=str(i + 1),
            callback_data=f"dl:{task_id}:{i}",
        )
        for i in range(len(result.tracks))
    ]
    # Разбиваем по 4
    for row_start in range(0, len(buttons), 4):
        builder.row(*buttons[row_start:row_start + 4])

    # Навигация
    nav_row = []
    if result.page > 1:
        nav_row.append(
            InlineKeyboardButton(text="⬅️", callback_data=f"page:{task_id}:{result.page - 1}")
        )
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="noop"))

    total_pages = max(1, -(-result.total // 8))
    nav_row.append(
        InlineKeyboardButton(
            text=f"{result.page} / {total_pages}",
            callback_data="noop",
        )
    )

    if result.has_next:
        nav_row.append(
            InlineKeyboardButton(text="➡️", callback_data=f"page:{task_id}:{result.page + 1}")
        )
    else:
        nav_row.append(InlineKeyboardButton(text=" ", callback_data="noop"))

    builder.row(*nav_row)

    # Закрыть
    builder.row(InlineKeyboardButton(text="❌ Закрыть", callback_data="close"))

    return builder.as_markup()


def build_track_list_text(result: SearchResult) -> str:
    """
    Текст сообщения со списком треков в стиле @vkmusic_bot:
      1. Artist – Title  2:34  4.7M  256k
      ...
    """
    lines = []
    for i, track in enumerate(result.tracks):
        duration = _format_duration(track.duration)
        size_mb  = round(track.size / 1024 / 1024, 1)
        lossless = "  Lossless" if getattr(track, "is_lossless", False) else ""
        lines.append(
            f"{i + 1}. {track.artist} – {track.title}  "
            f"{duration}  {size_mb}M  {track.bitrate}k{lossless}"
        )
    return "\n".join(lines)


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
