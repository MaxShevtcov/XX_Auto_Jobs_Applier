import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.task_queue import TaskQueue
from src.telegram.telegram_bot import HhApplierBot


def make_bot():
    router = MagicMock()
    router.control.return_value = ("@xx_feedback", 344)
    queue = TaskQueue()
    runner = MagicMock()
    runner.last_run_info.return_value = {"last_run": None}
    scheduler = MagicMock()
    scheduler.next_fire_times.return_value = []
    bot = HhApplierBot(
        secrets={"tg_token": "t", "tg_chat_id": "@c"},
        router=router,
        task_queue=queue,
        runner=runner,
        scheduler=scheduler,
        store=None,
    )
    service = MagicMock()
    service.generate = AsyncMock(return_value="Ваше сопроводительное письмо")
    bot.cover_letter_service = service

    bot._app = MagicMock()
    bot._app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    return bot


def make_update():
    update = MagicMock()
    update.effective_user.id = 111
    update.effective_user.is_bot = False
    update.effective_chat.type = "private"
    return update


def make_context(args=None):
    context = MagicMock()
    context.args = args or []
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    return context


async def wait_until(predicate, timeout=3.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


@pytest.mark.asyncio
class TestLetterCommand:
    async def test_empty_args_shows_hint(self):
        bot = make_bot()
        context = make_context()
        await bot.cmd_letter(make_update(), context)
        text = context.bot.send_message.await_args.kwargs["text"]
        assert "/letter" in text
        assert bot.task_queue.size() == 0

    async def test_letter_enqueues_task_with_position(self):
        bot = make_bot()
        await bot.task_queue.start()
        try:
            context = make_context(args=["hh.ru/vacancy/123"])
            await bot.cmd_letter(make_update(), context)

            text = context.bot.send_message.await_args.kwargs["text"]
            assert "🚀" in text or "очеред" in text.lower()

            done = await wait_until(
                lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0
            )
            assert done
        finally:
            await bot.task_queue.stop()

    async def test_result_sent_only_to_control_topic(self):
        """Результат /letter не дублируется в jobs-топик"""
        bot = make_bot()
        await bot.task_queue.start()
        try:
            context = make_context(args=["Текст вакансии"])
            await bot.cmd_letter(make_update(), context)

            ok = await wait_until(
                lambda: any(
                    "сопроводительное письмо" in call.kwargs.get("text", "").lower()
                    for call in bot._app.bot.send_message.await_args_list
                )
            )
            assert ok
            # все сообщения ушли в control-топик (344), ни одного в jobs (345)
            for call in bot._app.bot.send_message.await_args_list:
                assert call.kwargs.get("message_thread_id") == 344
        finally:
            await bot.task_queue.stop()

    async def test_letter_failure_sends_error_message(self):
        bot = make_bot()
        bot.cover_letter_service.generate = AsyncMock(side_effect=RuntimeError("LLM down"))
        await bot.task_queue.start()
        try:
            context = make_context(args=["вакансия"])
            await bot.cmd_letter(make_update(), context)
            ok = await wait_until(
                lambda: any(
                    "Ошибка" in call.kwargs.get("text", "")
                    for call in bot._app.bot.send_message.await_args_list
                )
            )
            assert ok
        finally:
            await bot.task_queue.stop()

    async def test_letter_queued_behind_running_search(self):
        """letter-задача, поставленная во время поиска, выполняется строго после него"""
        bot = make_bot()
        order = []

        async def fake_search(*a, **kwargs):
            order.append("search_start")
            await asyncio.sleep(0.1)
            order.append("search_end")
            return {"success_applies": 0, "stopped_reason": ""}

        bot.runner.run_search = AsyncMock(side_effect=fake_search)

        async def fake_generate(text):
            order.append("letter")
            return "письмо"

        bot.cover_letter_service.generate = AsyncMock(side_effect=fake_generate)

        await bot.task_queue.start()
        try:
            # ставим поиск
            await bot.cmd_search(make_update(), make_context())
            # сразу ставим письмо
            await bot.cmd_letter(make_update(), make_context(args=["вакансия"]))

            done = await wait_until(
                lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0
            )
            assert done
            assert order.index("letter") > order.index("search_end")
        finally:
            await bot.task_queue.stop()
