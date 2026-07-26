import asyncio
from pyrogram import Client

# ===========================
# Настройки
# ===========================
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

        print(f"\n📨 Отправляем запрос: {QUERY}\n")

        await app.send_message(BOT_USERNAME, QUERY)

        print("⏳ Ожидание ответа...\n")
        await asyncio.sleep(5)

        msg = None

        async for message in app.get_chat_history(BOT_USERNAME, limit=1):
            msg = message
            break

        if not msg:
            print("❌ Ответ не получен.")
            return

        print("=" * 80)
        print("ID:", msg.id)

        if msg.text:
            print("\nTEXT:")
            print(msg.text)

        if msg.caption:
            print("\nCAPTION:")
            print(msg.caption)

        if msg.audio:
            print("\nAUDIO:")
            print("Название :", msg.audio.file_name)
            print("Длительность:", msg.audio.duration)
            print("Размер:", msg.audio.file_size)

        print("\n================= RAW MESSAGE =================")
        print(msg)

        if msg.reply_markup:

            print("\n================= INLINE BUTTONS =================")

            for i, row in enumerate(msg.reply_markup.inline_keyboard):

                print(f"\nROW {i + 1}")

                for j, button in enumerate(row):

                    print("-" * 60)
                    print(f"BUTTON {j + 1}")

                    print("TEXT:", button.text)

                    print("CALLBACK_DATA:",
                          getattr(button, "callback_data", None))

                    print("URL:",
                          getattr(button, "url", None))

                    print("LOGIN_URL:",
                          getattr(button, "login_url", None))

                    print("WEB_APP:",
                          getattr(button, "web_app", None))

                    print("OBJECT:")
                    print(repr(button))
        else:
            print("\n❌ Inline-кнопок нет.")

        print("\n================= ATTRIBUTES =================")
        print(dir(msg))


if __name__ == "__main__":
    asyncio.run(main())