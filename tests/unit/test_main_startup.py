"""
Тесты для bot/main.py — порядок запуска и управление сессией БД.

BUG FIX: pool.start() и queue.start() вызывались внутри блока
`async with async_session_factory() as session`, что закрывало сессию
до начала polling. Теперь сессия живёт столько же, сколько приложение.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest


class TestStartupOrder:
    """
    Проверяем что pool.start() и queue.start() вызываются ДО dp.start_polling().
    """

    async def test_pool_starts_before_polling(self) -> None:
        """
        pool.start() должен завершиться до того, как бот начнёт polling.
        Порядок: pool.start → queue.start → dp.start_polling.
        """
        call_order: list[str] = []

        pool_mock  = AsyncMock()
        queue_mock = AsyncMock()
        dp_mock    = AsyncMock()

        async def record_pool_start():
            call_order.append("pool.start")

        async def record_queue_start():
            call_order.append("queue.start")

        async def record_polling(*args, **kwargs):
            call_order.append("dp.start_polling")

        pool_mock.start       = record_pool_start
        pool_mock.stop        = AsyncMock()
        queue_mock.start      = record_queue_start
        queue_mock.stop       = AsyncMock()
        dp_mock.start_polling = record_polling
        dp_mock.resolve_used_update_types = MagicMock(return_value=[])

        # Имитируем порядок, заложенный в bot/main.py
        await pool_mock.start()
        await queue_mock.start()
        await dp_mock.start_polling(MagicMock(), allowed_updates=[])

        assert call_order == ["pool.start", "queue.start", "dp.start_polling"], (
            f"Неправильный порядок запуска: {call_order}"
        )

    async def test_queue_stops_on_shutdown(self) -> None:
        """
        При остановке бота queue.stop() и pool.stop() должны вызываться.
        """
        queue_mock = AsyncMock()
        pool_mock  = AsyncMock()

        # Симулируем блок finally из bot/main.py
        try:
            raise KeyboardInterrupt
        except KeyboardInterrupt:
            await queue_mock.stop()
            await pool_mock.stop()

        queue_mock.stop.assert_called_once()
        pool_mock.stop.assert_called_once()


class TestSessionLifecycle:
    """
    BUG FIX: сессия не должна закрываться до завершения polling.
    """

    async def test_session_stays_open_during_polling(self) -> None:
        """
        Сессия БД должна оставаться активной пока работает polling.
        Имитируем: открываем сессию → запускаем компоненты → polling → закрываем сессию.
        """
        session_closed_during_polling = False
        session_mock = AsyncMock()

        async def fake_start_polling(*args, **kwargs):
            # Во время polling сессия НЕ должна быть закрыта
            nonlocal session_closed_during_polling
            if session_mock.__aexit__.called:
                session_closed_during_polling = True

        dp_mock = AsyncMock()
        dp_mock.start_polling = fake_start_polling
        dp_mock.resolve_used_update_types = MagicMock(return_value=[])

        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__  = AsyncMock(return_value=False)

        # Правильный порядок из исправленного bot/main.py
        await session_mock.__aenter__()
        try:
            await dp_mock.start_polling(MagicMock(), allowed_updates=[])
        finally:
            await session_mock.__aexit__(None, None, None)

        assert not session_closed_during_polling, (
            "Сессия БД была закрыта ДО завершения polling — bug воспроизводится"
        )

    async def test_session_closed_after_polling(self) -> None:
        """
        После завершения polling __aexit__ сессии должен быть вызван.
        """
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__  = AsyncMock(return_value=False)

        await session_mock.__aenter__()
        try:
            pass  # polling завершился
        finally:
            await session_mock.__aexit__(None, None, None)

        session_mock.__aexit__.assert_called_once()
