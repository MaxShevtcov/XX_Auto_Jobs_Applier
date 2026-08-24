import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram.progress_messenger import ProgressMessenger


def make_messenger(throttle=10.0):
    bot = MagicMock()
    bot.send_message = AsyncMock(return_value=MagicMock(message_id=42))
    bot.edit_message_text = AsyncMock()
    messenger = ProgressMessenger(
        bot=bot,
        chat_id="@xx_feedback",
        thread_id=344,
        throttle_sec=throttle,
    )
    return messenger, bot


@pytest.mark.asyncio
class TestProgressMessenger:
    async def test_start_sends_status_message(self):
        messenger, bot = make_messenger()
        await messenger.start()
        bot.send_message.assert_awaited_once()
        kwargs = bot.send_message.await_args.kwargs
        assert kwargs["message_thread_id"] == 344

    async def test_on_stage_edits_message(self):
        messenger, bot = make_messenger(throttle=0)
        await messenger.start()
        await messenger.on_stage("resume", "Сбор резюме")
        assert bot.edit_message_text.await_count >= 1

    async def test_finish_replaces_text_without_throttle(self):
        messenger, bot = make_messenger()
        await messenger.start()
        await messenger.finish("✅ Готово")
        last_call = bot.edit_message_text.await_args
        assert "Готово" in last_call.kwargs["text"]

    async def test_progress_messenger_throttles_edits(self):
        messenger, bot = make_messenger(throttle=0.5)
        await messenger.start()
        await asyncio.sleep(0.55)
        await messenger.on_stage("a", "1")
        await messenger.on_stage("b", "2")  # раньше троттлинга — глотается
        await asyncio.sleep(0.55)
        await messenger.on_stage("c", "3")  # после паузы — проходит
        assert bot.edit_message_text.await_count == 2

    async def test_errors_are_swallowed(self):
        messenger, bot = make_messenger()
        bot.send_message = AsyncMock(side_effect=RuntimeError("tg down"))
        bot.edit_message_text = AsyncMock(side_effect=RuntimeError("tg down"))
        await messenger.start()  # не бросает
        await messenger.on_stage("x", "y")
        await messenger.finish("done")

    async def test_start_only_once(self):
        messenger, bot = make_messenger()
        await messenger.start()
        await messenger.start()
        assert bot.send_message.await_count == 1
