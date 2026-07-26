import asyncio
from pyrogram import Client

API_ID = "21037821"  
API_HASH = "56c08e125c32984ea6532dc64d5edcc8"  
BOT_TOKEN = "8441812303:AAHexWFn90-n_gosQGnF7f9uyEtsYEoq0Q8" 
BOT_USERNAME = "vkmusic_bot"   # если username другой - замени
QUERY = "Mulk"


async def main():
    async with Client(
        "userbot",
        api_id=API_ID,
        api_hash=API_HASH
    ) as app:

        print(f"Отправляю запрос: {QUERY}")
        await app.send_message(BOT_USERNAME, QUERY)

        print("Жду ответ...")
        await asyncio.sleep(5)

        print("\nПоследние сообщения:\n")

        async for msg in app.get_chat_history(BOT_USERNAME, limit=5):

            print("=" * 50)
            print("ID:", msg.id)

            if msg.text:
                print("TEXT:")
                print(msg.text)

            if msg.caption:
                print("CAPTION:")
                print(msg.caption)

            if msg.audio:
                print("AUDIO:")
                print(msg.audio.file_name)
                print(msg.audio.duration)
                print(msg.audio.file_size)

            if msg.reply_markup:
                print("INLINE BUTTONS:")
                for row in msg.reply_markup.inline_keyboard:
                    print([button.text for button in row])


if __name__ == "__main__":
    asyncio.run(main())