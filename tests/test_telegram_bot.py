from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.task_queue import Task, TaskQueue
from src.telegram.telegram_bot import HhApplierBot, build_menu_keyboard


def make_update(user_id=111, chat_id=-100999, chat_username="xx_feedback"):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat.id = chat_id
    update.effective_chat.username = chat_username
    update.effective_chat.type = "supergroup"
    update.effective_message.reply_text = AsyncMock()
    return update


def make_context():
    context = MagicMock()
    context.bot.send_message = AsyncMock()
    return context


SECRETS = {
    "tg_token": "test_token",
    "tg_chat_id": "@xx_feedback",
    "tg_report_topic_id": 344,
    "tg_err_topic_id": 5,
}


@pytest.mark.asyncio
class TestHhApplierBot:
    def _make_bot(self, secrets=None, allowed=None):
        secrets = dict(secrets or SECRETS)
        router = MagicMock()
        router.control.return_value = ("@xx_feedback", 344)
        queue = TaskQueue()
        runner = MagicMock()
        runner.last_run_info.return_value = {"last_run": None, "success_applies_num": 0}
        scheduler = MagicMock()
        scheduler.next_fire_times.return_value = []
        bot = HhApplierBot(
            secrets=secrets,
            router=router,
            task_queue=queue,
            runner=runner,
            scheduler=scheduler,
            store=None,
            allowed_user_ids=allowed or [],
        )
        return bot

    async def test_unauthorized_update_ignored(self):
        bot = self._make_bot(allowed=[222])
        update = make_update(user_id=111)
        context = make_context()

        await bot.cmd_start(update, context)

        context.bot.send_message.assert_not_awaited()
        update.effective_message.reply_text.assert_not_awaited()

    async def test_authorized_by_allowlist(self):
        bot = self._make_bot(allowed=[111])
        update = make_update(user_id=111)
        context = make_context()

        await bot.cmd_start(update, context)
        context.bot.send_message.assert_awaited_once()
        _, kwargs = context.bot.send_message.await_args
        assert kwargs["message_thread_id"] == 344

    async def test_status_reports_idle_and_queue_length(self):
        bot = self._make_bot()
        update = make_update()
        context = make_context()

        await bot.cmd_status(update, context)
        text = context.bot.send_message.await_args.kwargs["text"]
        assert "Свободен" in text or "свободен" in text
        assert "0" in text

    async def test_status_reports_busy_with_source(self):
        bot = self._make_bot()

        async def long_task():
            pass

        task = Task(source="manual_search", coro_factory=lambda: long_task())
        # имитируем занятость напрямую через внутреннее состояние очереди
        bot.task_queue._current = task
        try:
            update = make_update()
            context = make_context()
            await bot.cmd_status(update, context)
            text = context.bot.send_message.await_args.kwargs["text"]
            assert "manual_search" in text
        finally:
            bot.task_queue._current = None

    async def test_menu_shows_inline_keyboard(self):
        bot = self._make_bot()
        update = make_update()
        context = make_context()

        await bot.cmd_menu(update, context)
        _, kwargs = context.bot.send_message.await_args
        markup = kwargs.get("reply_markup")
        assert markup is not None
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert "search:now" in buttons
        assert "schedule:view" in buttons
        assert "schedule:toggle" in buttons
        assert "status:view" in buttons


class TestBuildMenuKeyboard:
    def test_keyboard_contains_all_actions(self):
        markup = build_menu_keyboard(enabled=True)
        buttons = [btn.callback_data for row in markup.inline_keyboard for btn in row]
        assert set(buttons) == {
            "search:now",
            "schedule:view",
            "schedule:set",
            "schedule:toggle",
            "status:view",
        }
