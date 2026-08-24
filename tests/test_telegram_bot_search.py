import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.task_queue import TaskQueue
from src.telegram.telegram_bot import HhApplierBot


def make_bot(daily_limit_reached=False):
    router = MagicMock()
    router.control.return_value = ("@xx_feedback", 344)
    queue = TaskQueue()
    runner = MagicMock()
    last_run = datetime.now().isoformat() if daily_limit_reached else "2020-01-01T00:00:00"
    runner.last_run_info.return_value = {"last_run": last_run, "success_applies_num": 0}
    runner.run_search = AsyncMock(return_value={"success_applies": 5, "stopped_reason": ""})
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
    # мок-бот Telegram для отправки вне хендлеров
    bot._app = MagicMock()
    bot._app.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    bot._app.bot.edit_message_text = AsyncMock()
    return bot


def make_update(user_id=111):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.is_bot = False
    update.effective_chat.id = -100999
    update.effective_chat.username = None
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
class TestSearchCommand:
    async def test_search_enqueues_manual_task(self):
        bot = make_bot()
        await bot.task_queue.start()
        try:
            update, context = make_update(), make_context()
            await bot.cmd_search(update, context)

            current_or_pending = bot.task_queue.size() >= 1 or bot.task_queue.is_busy
            assert current_or_pending
            await wait_until(lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0)
        finally:
            await bot.task_queue.stop()

    async def test_search_reports_queue_position_when_busy(self):
        bot = make_bot()
        await bot.task_queue.start()

        async def blocker():
            await asyncio.sleep(0.2)

        bot.task_queue.enqueue(
            __import__("src.application.task_queue", fromlist=["Task"]).Task(
                source="manual_search", coro_factory=blocker
            )
        )
        await wait_until(lambda: bot.task_queue.is_busy)

        update, context = make_update(), make_context()
        await bot.cmd_search(update, context)

        text = context.bot.send_message.await_args.kwargs["text"]
        assert "очеред" in text.lower()
        await wait_until(lambda: not bot.task_queue.is_busy)
        await bot.task_queue.stop()

    async def test_search_reports_immediate_start_at_position_one(self):
        bot = make_bot()
        await bot.task_queue.start()
        try:
            update, context = make_update(), make_context()
            await bot.cmd_search(update, context)
            text = context.bot.send_message.await_args.kwargs["text"]
            assert "🚀" in text
            await wait_until(lambda: not bot.task_queue.is_busy)
        finally:
            await bot.task_queue.stop()

    async def test_search_rejects_on_queue_overflow(self):
        bot = make_bot(maxsize := None)  # noqa
        bot.task_queue = TaskQueue(maxsize=1)
        await bot.task_queue.start()

        async def blocker():
            await asyncio.sleep(0.2)

        from src.application.task_queue import Task

        bot.task_queue.enqueue(Task(source="scheduled", coro_factory=blocker))
        await wait_until(lambda: bot.task_queue.is_busy)
        bot.task_queue.enqueue(Task(source="letter", coro_factory=blocker))

        update, context = make_update(), make_context()
        await bot.cmd_search(update, context)
        text = context.bot.send_message.await_args.kwargs["text"]
        assert "переполнен" in text.lower()
        await wait_until(lambda: not bot.task_queue.is_busy)
        await bot.task_queue.stop()

    async def test_search_requires_confirmation_without_force(self):
        bot = make_bot(daily_limit_reached=True)
        update, context = make_update(), make_context()
        await bot.cmd_search(update, context)

        # задача НЕ поставлена; отправлен запрос подтверждения с кнопками
        assert bot.task_queue.size() == 0
        markup = context.bot.send_message.await_args.kwargs.get("reply_markup")
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "search:confirm:force" in buttons

    async def test_search_force_argument_skips_confirmation(self):
        bot = make_bot(daily_limit_reached=True)
        await bot.task_queue.start()
        try:
            update, context = make_update(), make_context(args=["force"])
            await bot.cmd_search(update, context)
            assert bot.task_queue.size() >= 1
            await wait_until(lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0)
        finally:
            await bot.task_queue.stop()

    async def test_search_confirm_force_callback_starts_task(self):
        bot = make_bot(daily_limit_reached=True)
        await bot.task_queue.start()
        try:
            update = make_update()
            query = MagicMock()
            query.data = "search:confirm:force"
            query.answer = AsyncMock()
            update.callback_query = query
            context = make_context()
            await bot.on_callback(update, context)
            assert bot.task_queue.size() >= 1
            await wait_until(lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0)
        finally:
            await bot.task_queue.stop()

    async def test_search_cancel_callback_does_not_enqueue(self):
        bot = make_bot(daily_limit_reached=True)
        update = make_update()
        query = MagicMock()
        query.data = "search:cancel"
        query.answer = AsyncMock()
        update.callback_query = query
        context = make_context()
        await bot.on_callback(update, context)
        assert bot.task_queue.size() == 0

    async def test_search_reports_result_via_on_done(self):
        bot = make_bot()
        await bot.task_queue.start()
        try:
            update, context = make_update(), make_context()
            await bot.cmd_search(update, context)
            done = await wait_until(lambda: bot._app.bot.send_message.await_count >= 2)
            assert done
            texts = [
                call.kwargs.get("text", "") for call in bot._app.bot.send_message.await_args_list
            ]
            assert any("5" in t for t in texts)  # количество откликов в сводке
        finally:
            await bot.task_queue.stop()

    async def test_search_failure_sends_error_message(self):
        bot = make_bot()
        bot.runner.run_search = AsyncMock(side_effect=RuntimeError("boom"))
        await bot.task_queue.start()
        try:
            update, context = make_update(), make_context()
            await bot.cmd_search(update, context)
            ok = await wait_until(
                lambda: any(
                    "Ошибка" in call.kwargs.get("text", "")
                    for call in bot._app.bot.send_message.await_args_list
                )
            )
            assert ok
        finally:
            await bot.task_queue.stop()

    async def test_progress_starts_only_when_task_begins(self):
        bot = make_bot()
        await bot.task_queue.start()

        async def blocker():
            await asyncio.sleep(0.15)

        from src.application.task_queue import Task

        bot.task_queue.enqueue(Task(source="manual_search", coro_factory=blocker))
        await wait_until(lambda: bot.task_queue.is_busy)

        update, context = make_update(), make_context()
        await bot.cmd_search(update, context)

        # пока задача ждёт очереди — статусное сообщение не создано
        assert bot._app.bot.send_message.await_count == 0

        await wait_until(lambda: not bot.task_queue.is_busy and bot.task_queue.size() == 0)
        await bot.task_queue.stop()
        # после старта задачи прогресс начался
        assert bot._app.bot.send_message.await_count >= 1
