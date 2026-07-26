    from infrastructure.database.repositories.userbot_repo import UserbotRepository
    from infrastructure.database.session import async_session_factory

    async with async_session_factory() as session_db:
        repo = UserbotRepository(session_db)

        # BUG FIX: check for duplicate phone before INSERT to avoid IntegrityError
        existing = await repo.get_by_phone(data["phone"])
        if existing:
            await message.answer(
                f"⚠️ Userbot с номером {data['phone']} уже существует (#{existing.id}).\n"
                "Удалите его сначала или используйте другой номер."
            )
            return

        userbot = await repo.create(
            phone=data["phone"],
            api_id=data["api_id"],
            api_hash=data["api_hash"],
            session_string=session,
        )

    ok = await pool.add_userbot(userbot.id)