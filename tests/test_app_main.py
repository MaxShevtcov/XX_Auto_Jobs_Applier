from unittest.mock import AsyncMock, MagicMock

import pytest

import app_main
from src.application.search_scheduler import SearchScheduler
from src.application.task_queue import TaskQueue
from src.telegram.telegram_bot import HhApplierBot

VALID_SECRETS = {
    "hh_login": "l",
    "hh_password": "p",
    "llm_api_key": "k",
    "llm_proxy": ["proxy1"],
    "tg_token": "test_token",
    "tg_chat_id": "@xx_feedback",
    "tg_report_topic_id": 344,
}


@pytest.fixture
def mock_all_components(mocker):
    manager = MagicMock()
    manager.initialize = AsyncMock()
    manager.close = AsyncMock()
    mocker.patch("app_main.PlaywrightJobManager", return_value=manager)

    secrets = {
        **VALID_SECRETS,
        "tg_err_topic_id": 5,
    }
    validator = MagicMock()
    validator.validate_secrets.return_value = secrets
    validator.validate_search_config.return_value = {"job_title": "dev"}
    mocker.patch("app_main.ConfigValidator", return_value=validator)
    mocker.patch("main.ConfigValidator", return_value=validator)
    mocker.patch("app_main.load_raw_secrets", return_value=dict(secrets))
    mocker.patch("main.FileManager.validate_data_folder", return_value=None)
    mocker.patch("src.telegram.ptb_request.build_ptb_request", return_value=MagicMock())

    return {"manager": manager, "secrets": secrets}


@pytest.mark.asyncio
class TestBuildApplication:
    async def test_build_application_creates_components(self, mock_all_components):
        task_queue, runner, scheduler, bot = await app_main.build_application()

        assert isinstance(task_queue, TaskQueue)
        assert isinstance(scheduler, SearchScheduler)
        assert isinstance(bot, HhApplierBot)
        assert scheduler.scheduler is not None  # APScheduler запущен

        await scheduler.shutdown()
        await task_queue.stop()

    @pytest.mark.asyncio
    async def test_manager_factory_creates_initialized_manager(self, mock_all_components):
        factory = app_main.build_manager_factory(mock_all_components["secrets"])
        manager = await factory()
        manager.initialize.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_bot_uses_allowlist_from_secrets(self, mock_all_components):
        mock_all_components["secrets"]["tg_allowed_user_ids"] = [111]
        task_queue, runner, scheduler, bot = await app_main.build_application()
        assert bot.allowed_user_ids == [111]
        await scheduler.shutdown()
        await task_queue.stop()

    @pytest.mark.asyncio
    async def test_notify_errors_sends_to_err_topic(self, mock_all_components, mocker):
        send_mock = AsyncMock()
        mocker.patch("telegram.Bot.send_message", new=send_mock)
        task_queue, runner, scheduler, bot = await app_main.build_application()

        # достаём notify_errors через замыкание шедулера
        await scheduler.errors_notifier("тест")
        send_mock.assert_awaited_once()
        kwargs = send_mock.await_args.kwargs
        assert kwargs["message_thread_id"] == 5
        await scheduler.shutdown()
        await task_queue.stop()


class TestCreateApp:
    def test_create_app_registers_handlers(self):
        from telegram.ext import Application

        secrets = {
            "tg_token": "123:test",
            "tg_chat_id": "@c",
            "llm_proxy": [],
        }
        router = MagicMock()
        router.control.return_value = ("@c", None)
        bot = HhApplierBot(
            secrets=secrets,
            router=router,
            task_queue=MagicMock(),
            runner=MagicMock(),
        )
        app = bot.create_app()
        assert isinstance(app, Application)
        assert bot._app is app
        handler_types = [type(h) for group in app.handlers.values() for h in group]
        from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler

        assert CommandHandler in handler_types
        assert CallbackQueryHandler in handler_types
        assert MessageHandler in handler_types
