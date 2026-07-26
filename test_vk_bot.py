import asyncio
from pyrogram import Client

# ===========================
# Настройки
# ===========================
API_ID = "21037821"  
API_HASH = "56c08e125c32984ea6532dc64d5edcc8" 

BOT_USERNAME = "vkmusic_bot"
QUERY = "Mulk"
TARGET_CHAT = 8182421826

async def main():
    async with Client(
        "userbot",
        api_id=API_ID,
        api_hash=API_HASH
    ) as app:

        print(f"📨 Отправляем запрос: {QUERY}")
        await app.send_message(BOT_USERNAME, QUERY)

        print("⏳ Ждем ответ...")
        await asyncio.sleep(5)

        msg = None

        async for m in app.get_chat_history(BOT_USERNAME, limit=1):
            msg = m
            break

        if not msg:
            print("❌ Ответ не получен.")
            return

        print("\n==============================")
        print("Получено сообщение:")
        print("==============================")

        if msg.text:
            print(msg.text)

        if not msg.reply_markup:
            print("❌ Нет inline-кнопок.")
            return

        print("\n🎯 Нажимаем первую кнопку...")

        try:
            result = await msg.click(0)

            print("✅ Кнопка нажата.")
            print("Ответ:", result)

        except Exception as e:
            print("❌ Ошибка при нажатии:")
            print(type(e).__name__, e)
            return

        print("\n⏳ Ждем ответ после нажатия...")
        await asyncio.sleep(8)

        print("\n==============================")
        print("Последние сообщения")
        print("==============================")

        async for m in app.get_chat_history(BOT_USERNAME, limit=5):

            print("-" * 60)
            print("ID:", m.id)

            if m.text:
                print("TEXT:")
                print(m.text)

            if m.caption:
                print("CAPTION:")
                print(m.caption)

            if m.audio:
                print("\n🎵 АУДИО ПОЛУЧЕНО!")
                print("Название :", m.audio.file_name)
                print("Длительность :", m.audio.duration)
                print("Размер :", m.audio.file_size)

                
                await app.send_audio(
                    chat_id=TARGET_CHAT,
                    audio=m.audio.file_id,
                    caption="🎵 Ваш трек"
                )


                print("\n✅ Файл j Отправляем:")
                print(filename)

                break


if __name__ == "__main__":
    asyncio.run(main())