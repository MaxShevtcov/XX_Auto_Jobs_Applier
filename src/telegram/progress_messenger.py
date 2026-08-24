import time

from src.logger_config import logger


class ProgressMessenger:
    """
    Live-статус задачи в control-топике: создаёт одно сообщение и
    редактирует его по мере этапов. Троттлинг edit_text >= throttle_sec,
    ошибки Telegram глотаются (прогресс не должен ломать задачу).
    """

    STAGE_TEXTS = {
        "login": "🔑 Вход на hh.ru...",
        "parameters": "⚙️ Подготовка параметров поиска...",
        "resume": "📄 Сбор информации о резюме...",
        "search": "🔍 Поиск вакансий...",
    }

    def __init__(
        self,
        bot,
        chat_id,
        thread_id=None,
        throttle_sec: float = 10.0,
    ):
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.throttle_sec = throttle_sec
        self.message_id = None
        self._last_edit_ts = 0.0

    async def start(self, text: str = "🚀 Задача запущена") -> None:
        """Отправить стартовое статусное сообщение (один раз)"""
        if self.message_id is not None:
            return
        try:
            message = await self.bot.send_message(
                chat_id=self.chat_id,
                message_thread_id=self.thread_id,
                text=text,
            )
            self.message_id = message.message_id
            self._last_edit_ts = time.monotonic()
        except Exception as e:
            logger.warning(f"ProgressMessenger: не удалось отправить статус: {e}")

    async def on_stage(self, stage: str, detail: str) -> None:
        """Callback для run_search_pipeline: троттлированное редактирование статуса"""
        text = self.STAGE_TEXTS.get(stage, f"⏳ {detail or stage}")
        await self._edit(text, force=False)

    async def finish(self, text: str) -> None:
        """Финальный статус — без троттлинга"""
        await self._edit(text, force=True)

    async def _edit(self, text: str, force: bool) -> None:
        if self.message_id is None:
            return
        now = time.monotonic()
        if not force and (now - self._last_edit_ts) < self.throttle_sec:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
            self._last_edit_ts = now
        except Exception as e:
            logger.warning(f"ProgressMessenger: не удалось обновить статус: {e}")
