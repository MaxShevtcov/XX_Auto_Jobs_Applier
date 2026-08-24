from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.schedule_store import ScheduleStore
from src.telegram.telegram_bot import HhApplierBot


def make_bot(store=None):
    router = MagicMock()
    router.control.return_value = ("@xx_feedback", 344)
    queue = MagicMock()
    queue.is_busy = False
    queue.size.return_value = 0
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
        store=store,
    )
    bot._app = MagicMock()
    return bot


def make_update(user_id=111):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.is_bot = False
    update.effective_chat.id = -100999
    update.effective_chat.type = "private"
    update.effective_message.text = ""
    update.callback_query = None
    return update


def make_context():
    context = MagicMock()
    context.bot.send_message = AsyncMock(return_value=MagicMock(message_id=1))
    return context


@pytest.mark.asyncio
class TestScheduleView:
    async def test_view_shows_next_fire_times(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)
        times = [
            __import__("datetime").datetime(2026, 8, 25, 9, 0),
            __import__("datetime").datetime(2026, 8, 26, 9, 0),
            __import__("datetime").datetime(2026, 8, 27, 9, 0),
        ]
        bot.scheduler.next_fire_times.return_value = times

        update, context = make_update(), make_context()
        await bot.on_schedule_view(update, context)

        text = context.bot.send_message.await_args.kwargs["text"]
        assert "0 9 * * *" in text
        assert "Europe/Kaliningrad" in text
        markup = context.bot.send_message.await_args.kwargs.get("reply_markup")
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert "schedule:set" in buttons
        assert "schedule:toggle" in buttons


@pytest.mark.asyncio
class TestScheduleSetConversation:
    async def test_set_starts_awaiting_input(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)

        await bot.on_schedule_set(make_update(), make_context())

        assert bot._pending_schedule_input is True

    async def test_invalid_input_keeps_waiting_with_error(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)
        await bot.on_schedule_set(make_update(), make_context())

        update = make_update()
        update.effective_message.text = "завтра утром"
        context = make_context()
        await bot._on_text(update, context)

        text = context.bot.send_message.await_args.kwargs["text"]
        assert "cron" in text.lower() or "Некорректн" in text
        assert bot._pending_schedule_input is True

    async def test_valid_input_shows_preview_with_apply_button(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)
        await bot.on_schedule_set(make_update(), make_context())

        update = make_update()
        update.effective_message.text = "30 8 * * * Europe/Moscow"
        context = make_context()
        await bot._on_text(update, context)

        kwargs = context.bot.send_message.await_args.kwargs
        assert "Превью" in kwargs["text"] or "следующие запуски" in kwargs["text"].lower()
        markup = kwargs.get("reply_markup")
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert any(b.startswith("schedule:apply:") for b in buttons)
        # ожидание ввода снято, кандидат сохранён
        assert bot._pending_schedule_input is False
        assert bot._pending_schedule_cfg is not None

    async def test_text_ignored_when_not_awaiting(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)
        context = make_context()
        await bot._on_text(make_update(), context)
        context.bot.send_message.assert_not_awaited()

    async def test_set_conversation_saves_and_applies(self, tmp_path):
        path = tmp_path / "s.yaml"
        store = ScheduleStore(path)
        bot = make_bot(store=store)
        await bot.on_schedule_set(make_update(), make_context())

        update = make_update()
        update.effective_message.text = "15 10 * * *"
        context = make_context()
        await bot._on_text(update, context)

        cfg_hash = bot._cfg_hash(bot._pending_schedule_cfg)
        query_update = make_update()
        query = MagicMock()
        query.data = f"schedule:apply:{cfg_hash}"
        query.answer = AsyncMock()
        query_update.callback_query = query

        await bot.on_callback(query_update, make_context())

        # файл записан
        loaded = store.load()
        assert loaded.cron == "15 10 * * *"
        # job пересоздан в шедулере
        bot.scheduler.apply_schedule.assert_called_once()

    async def test_cancel_clears_state(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        bot = make_bot(store=store)
        await bot.on_schedule_set(make_update(), make_context())
        update = make_update()
        update.effective_message.text = "09:30"
        await bot._on_text(update, make_context())

        query_update = make_update()
        query = MagicMock()
        query.data = "schedule:cancel"
        query.answer = AsyncMock()
        query_update.callback_query = query
        await bot.on_callback(query_update, make_context())

        assert bot._pending_schedule_cfg is None


@pytest.mark.asyncio
class TestScheduleToggle:
    async def test_toggle_off_removes_job_keeps_file(self, tmp_path):
        path = tmp_path / "s.yaml"
        store = ScheduleStore(path)
        bot = make_bot(store=store)

        await bot.on_schedule_toggle(make_update(), make_context())

        cfg = store.load()
        assert cfg.enabled is False  # файл сохранён с enabled=false
        bot.scheduler.apply_schedule.assert_called_once()
        applied_cfg = bot.scheduler.apply_schedule.call_args.args[0]
        assert applied_cfg.enabled is False

    async def test_toggle_on_restores_job(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text("enabled: false\ncron: '0 9 * * *'\n", encoding="utf-8")
        store = ScheduleStore(path)
        bot = make_bot(store=store)

        await bot.on_schedule_toggle(make_update(), make_context())

        assert store.load().enabled is True
        bot.scheduler.apply_schedule.assert_called_once()


@pytest.mark.asyncio
class TestStatusDisabledSchedule:
    async def test_status_shows_disabled_message(self, tmp_path):
        path = tmp_path / "s.yaml"
        path.write_text("enabled: false\n", encoding="utf-8")
        store = ScheduleStore(path)
        bot = make_bot(store=store)
        bot.scheduler.next_fire_times.return_value = []

        text = bot.build_status_text()
        assert "Автозапуск отключён" in text
