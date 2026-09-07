"""
Long-running точка входа: Telegram-бот + встроенный шедулер (APScheduler)
+ последовательная очередь задач в одном процессе и event loop.
"""
import asyncio
import signal
import traceback
from pathlib import Path

from main import ConfigError, ConfigValidator, FileManager
from src.application.schedule_store import ScheduleStore
from src.application.search_runner import SearchRunner
from src.application.search_scheduler import SearchScheduler
from src.application.task_queue import TaskQueue
from src.constants import SEARCH_CONFIG_FILE, SECRETS_FILE
from src.job_manager.playwright_manager import PlaywrightJobManager
from src.logger_config import logger
from src.telegram.ptb_request import load_raw_secrets
from src.telegram.job_search import TelegramJobScheduler, TelegramJobSearchRunner
from src.telegram.telegram_bot import HhApplierBot, TopicRouter


def build_manager_factory(secrets: dict):
    """
    Ленивая фабрика менеджера браузера: создаёт менеджер при первом обращении;
    при краше браузера SearchRunner сбрасывает ссылку, фабрика создаёт новый.
    """

    async def factory() -> PlaywrightJobManager:
        manager = PlaywrightJobManager(secrets)
        await manager.initialize()
        return manager

    return factory


async def build_application():
    """Собрать все компоненты long-running приложения"""
    data_folder = Path("data_folder")
    FileManager.validate_data_folder(data_folder)

    config_validator = ConfigValidator()
    secrets_validated = config_validator.validate_secrets(SECRETS_FILE)
    parameters = config_validator.validate_search_config(SEARCH_CONFIG_FILE, "", secrets_validated)
    # сырые секреты: содержат tg_chat_id/топики, которые pydantic-модель отбрасывает
    raw_secrets = load_raw_secrets(SECRETS_FILE)

    task_queue = TaskQueue()
    await task_queue.start()

    runner = SearchRunner(
        secrets=secrets_validated,
        parameters=parameters,
        llm_api_key=secrets_validated["llm_api_key"],
        llm_proxy=secrets_validated["llm_proxy"],
        manager_factory=build_manager_factory(secrets_validated),
    )

    store = ScheduleStore()

    async def notify_errors(message: str) -> None:
        """Короткое сообщение о критической ошибке шедулера/бота в err-топик"""
        from src.telegram.ptb_request import build_ptb_request
        from telegram import Bot

        bot = Bot(token=raw_secrets["tg_token"], request=build_ptb_request(raw_secrets))
        chat_id = raw_secrets.get("tg_chat_id")
        err_topic = raw_secrets.get("tg_err_topic_id")
        try:
            await bot.send_message(chat_id=chat_id, message_thread_id=err_topic, text=message)
        except Exception as e:
            logger.error(f"Не удалось отправить ошибку в err-топик: {e}")

    scheduler = SearchScheduler(task_queue, store, runner=runner, errors_notifier=notify_errors)
    await scheduler.start()

    from src.llm.cover_letter_service import CoverLetterService

    cover_letter_service = CoverLetterService(
        llm_api_key=secrets_validated["llm_api_key"],
        llm_proxy=secrets_validated["llm_proxy"],
        manager_factory=runner.get_manager,  # общий менеджер браузера на процесс
    )

    telegram_search_runner = TelegramJobSearchRunner(
        parameters=parameters,
        cover_letter_service=cover_letter_service,
    )
    telegram_search_scheduler = TelegramJobScheduler(task_queue, telegram_search_runner)

    router = TopicRouter(raw_secrets)
    bot = HhApplierBot(
        secrets=raw_secrets,
        router=router,
        task_queue=task_queue,
        runner=runner,
        scheduler=scheduler,
        store=store,
        allowed_user_ids=secrets_validated.get("tg_allowed_user_ids") or [],
        cover_letter_service=cover_letter_service,
        telegram_search_runner=telegram_search_runner,
        telegram_search_scheduler=telegram_search_scheduler,
    )

    return task_queue, runner, scheduler, bot


async def run_forever() -> None:
    task_queue, runner, scheduler, bot = await build_application()
    telegram_search_scheduler = bot.telegram_search_scheduler

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request_stop():
        logger.info("Получен сигнал остановки, завершаем работу...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            # Windows: add_signal_handler не поддерживается для этих сигналов
            signal.signal(sig, lambda *_: stop_event.set())

    app = bot.create_app()
    try:
        await app.initialize()
        await app.start()
        await telegram_search_scheduler.start()
        await app.updater.start_polling(drop_pending_updates=True)
        logger.info("Бот запущен (long-running режим)")
        await stop_event.wait()
    finally:
        logger.info("Graceful shutdown: останавливаем бота, шедулер и очередь")
        try:
            await app.updater.stop()
        except Exception:
            pass
        try:
            await app.stop()
            await app.shutdown()
        except Exception:
            tb_str = traceback.format_exc()
            logger.error(f"Ошибка при остановке PTB-приложения:\n{tb_str}")
        await scheduler.shutdown()
        await telegram_search_scheduler.shutdown()
        await task_queue.stop()
        if runner._manager is not None:
            try:
                await runner._manager.close()
            except Exception as e:
                logger.error(f"Ошибка при закрытии менеджера браузера: {e}")
        logger.info("Работа завершена")


async def main() -> None:
    try:
        await run_forever()
    except ConfigError as ce:
        logger.error(f"Ошибка конфигурации: {str(ce)}")
    except FileNotFoundError as fnf:
        logger.error(f"Файл не найден: {str(fnf)}")
    except Exception:
        tb_str = traceback.format_exc()
        logger.error(f"Неизвестная ошибка\n{tb_str}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
