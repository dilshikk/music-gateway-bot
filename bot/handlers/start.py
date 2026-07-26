from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from bot.keyboards.main import build_main_keyboard
from infrastructure.database.models import User

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, user: User) -> None:
    name = message.from_user.first_name if message.from_user else "друг"
    premium_badge = " 👑" if user.premium else ""

    await message.answer(
        f"👋 Привет, <b>{name}</b>{premium_badge}!\n\n"
        "🎵 Я помогу найти и скачать любую музыку.\n\n"
        "Просто напиши название песни или исполнителя.",
        reply_markup=build_main_keyboard(user.premium),
    )


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "<b>Как пользоваться:</b>\n\n"
        "1. Напиши название трека или исполнителя\n"
        "2. Выбери нужный трек из списка\n"
        "3. Получи аудиофайл\n\n"
        "<b>Команды:</b>\n"
        "/start — главное меню\n"
        "/history — история поиска\n"
        "/favorites — избранные треки\n"
        "/popular — популярные запросы\n"
        "/settings — настройки\n"
        "/help — эта справка",
    )
