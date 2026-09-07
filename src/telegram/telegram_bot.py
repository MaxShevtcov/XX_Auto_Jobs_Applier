from typing import List, Optional, Tuple

from src.application.search_runner import SearchRunner
from src.application.task_queue import (
    SOURCE_MANUAL_SEARCH,
    SOURCE_MANUAL_TELEGRAM_SEARCH,
    Task,
    TaskQueue,
)
from src.logger_config import logger
from src.telegram.progress_messenger import ProgressMessenger
from src.telegram.ptb_request import build_ptb_request
from src.utils.utils import load_yaml_file
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes


class TopicRouter:
    """
    Маршрутизация сообщений по топикам форум-чата tg_chat_id.

    | Метод    | Топик                                                        |
    |----------|--------------------------------------------------------------|
    | control  | tg_control_topic_id → tg_report_topic_id → None (без thread) |
    | jobs     | tg_jobs_topic_id или None                                    |
    | errors   | tg_err_topic_id                                              |
    | report   | tg_report_topic_id                                           |
    """

    def __init__(self, secrets: dict):
        self.chat_id = secrets.get("tg_chat_id")
        self.control_topic_id = secrets.get("tg_control_topic_id")
        self.report_topic_id = secrets.get("tg_report_topic_id")
        self.jobs_topic_id = secrets.get("tg_jobs_topic_id")
        self.err_topic_id = secrets.get("tg_err_topic_id")

    def control(self) -> Tuple[str, Optional[int]]:
        """Команды, меню, статусы, прогресс, сводные итоги, /letter"""
        topic = self.control_topic_id if self.control_topic_id else self.report_topic_id
        return self.chat_id, int(topic) if topic else None

    def jobs(self) -> Optional[Tuple[str, int]]:
        """Карточки вакансий из пайплайна (send_job_description)"""
        if not self.jobs_topic_id:
            return None
        return self.chat_id, int(self.jobs_topic_id)

    def errors(self) -> Tuple[str, int]:
        """Критические ошибки шедулера/бота"""
        return self.chat_id, int(self.err_topic_id) if self.err_topic_id else None

    def report(self) -> Tuple[str, int]:
        """Детальный отчёт после прогона (существующий механизм)"""
        return self.chat_id, int(self.report_topic_id) if self.report_topic_id else None


MENU_TEXT = "Главное меню — выберите действие:"


def build_menu_keyboard(enabled: bool = True) -> InlineKeyboardMarkup:
    toggle_label = "🔴 Автозапуск: вкл (выключить?)" if enabled else "🟢 Автозапуск: выкл (включить?)"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🔍 Искать вакансии сейчас", callback_data="search:now")],
            [InlineKeyboardButton("📣 Искать в Telegram", callback_data="tgsearch:now")],
            [InlineKeyboardButton("⏰ Текущее расписание", callback_data="schedule:view")],
            [InlineKeyboardButton("✏️ Изменить расписание", callback_data="schedule:set")],
            [InlineKeyboardButton(toggle_label, callback_data="schedule:toggle")],
            [InlineKeyboardButton("📊 Статус", callback_data="status:view")],
        ]
    )


class HhApplierBot:
    """
    Telegram-бот управления автопоиском: /start, /menu, /status (+ /search и
    /letter в последующих фазах). Работает в форум-чате tg_chat_id,
    все интерактивные ответы идут через TopicRouter.control().
    """

    def __init__(
        self,
        secrets: dict,
        router: TopicRouter,
        task_queue: TaskQueue,
        runner: SearchRunner,
        scheduler=None,
        store=None,
        allowed_user_ids: Optional[List[int]] = None,
        cover_letter_service=None,
        telegram_search_runner=None,
        telegram_search_scheduler=None,
    ):
        self.secrets = secrets
        self.router = router
        self.task_queue = task_queue
        self.runner = runner
        self.scheduler = scheduler
        self.store = store
        self.cover_letter_service = cover_letter_service
        self.telegram_search_runner = telegram_search_runner
        self.telegram_search_scheduler = telegram_search_scheduler
        self.allowed_user_ids = [int(u) for u in (allowed_user_ids or []) if str(u).strip()]
        self.chat_id = secrets.get("tg_chat_id")
        self._app: Optional[Application] = None
        # FSM-lite для диалога установки расписания (один пользователь)
        self._pending_schedule_input = False
        self._pending_schedule_cfg = None

    @property
    def _bot(self):
        """Bot-объект для отправки вне обработчиков (из worker'а очереди)"""
        return self._app.bot if self._app is not None else None

    def _cfg_hash(self, cfg) -> str:
        """Стабильный хеш конфига для callback_data кнопки «Сохранить»"""
        import hashlib

        return hashlib.sha256(cfg.model_dump_json().encode("utf-8")).hexdigest()[:8]

    # ---------- Авторизация ----------

    def _authorized(self, update: Update) -> bool:
        """Один пользователь: allowlist tg_allowed_user_ids либо чат из tg_chat_id"""
        if update is None or update.effective_user is None:
            return False
        if update.effective_user.is_bot is True:
            return False
        if self.allowed_user_ids:
            return int(update.effective_user.id) in self.allowed_user_ids
        chat = update.effective_chat
        if chat is None:
            return False
        if chat.type == "private":
            return True
        expected = self.chat_id
        if isinstance(expected, str) and expected.startswith("@"):
            return getattr(chat, "username", None) == expected[1:]
        return str(chat.id) == str(expected)

    # ---------- Отправка ----------

    async def send_control(
        self, context: ContextTypes.DEFAULT_TYPE, text: str, reply_markup=None
    ) -> None:
        chat_id, thread_id = self.router.control()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramError as e:
            logger.error(f"Не удалось отправить сообщение в control-топик: {e}")

    async def send_control_direct(self, text: str, reply_markup=None) -> None:
        """Отправка в control-топик без context (из worker'а очереди)"""
        bot = self._bot
        if bot is None:
            logger.error("PTB-приложение ещё не создано, сообщение не отправлено")
            return
        chat_id, thread_id = self.router.control()
        try:
            await bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=text,
                reply_markup=reply_markup,
            )
        except TelegramError as e:
            logger.error(f"Не удалось отправить сообщение в control-топик: {e}")

    # ---------- Команды ----------

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.warning(f"Неавторизованный /start от user {update.effective_user.id}")
            return
        await self.send_control(
            context,
            "Привет! Я бот автопоиска вакансий на hh.ru.\n" + MENU_TEXT,
            reply_markup=build_menu_keyboard(enabled=self._schedule_enabled()),
        )

    async def cmd_menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.warning(f"Неавторизованный /menu от user {update.effective_user.id}")
            return
        await self.send_control(context, MENU_TEXT, reply_markup=self._menu_markup())

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.warning(f"Неавторизованный /status от user {update.effective_user.id}")
            return
        await self.send_control(context, self.build_status_text())

    def _menu_markup(self) -> InlineKeyboardMarkup:
        return build_menu_keyboard(enabled=self._schedule_enabled())

    def _schedule_enabled(self) -> bool:
        if self.store is None:
            return True
        try:
            return self.store.load().enabled
        except Exception:
            return True

    # ---------- Статус ----------

    def build_status_text(self) -> str:
        lines = []
        current = self.task_queue.current()
        if current is not None:
            lines.append(f"🔄 Выполняется задача: {current.source}")
        else:
            lines.append("🟢 Свободен")
        lines.append(f"📦 Задач в очереди: {self.task_queue.size()}")

        last_run = self.runner.last_run_info() or {}
        last_run_str = last_run.get("last_run")
        success_num = last_run.get("success_applies_num", 0)
        lines.append(
            f"🕘 Последний поиск: {last_run_str or 'не выполнялся'} "
            f"(успешных откликов: {success_num})"
        )

        if self.scheduler is not None and self._schedule_enabled():
            times = self.scheduler.next_fire_times(1)
            if times:
                lines.append(f"⏰ Следующий автозапуск: {times[0].strftime('%d.%m %H:%M %Z')}")
        else:
            lines.append("⏰ Автозапуск отключён")
        if self.telegram_search_scheduler is not None:
            next_tg = self.telegram_search_scheduler.next_fire_time()
            lines.append(
                f"📣 Следующая проверка Telegram: {next_tg.strftime('%d.%m %H:%M %Z') if next_tg else 'отключена'}"
            )
        return "\n".join(lines)

    # ---------- Callback-кнопки ----------

    async def on_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            logger.warning(f"Неавторизованный callback от user {update.effective_user.id}")
            return
        data = query.data or ""
        try:
            await query.answer()
        except TelegramError as e:
            logger.warning(f"Не удалось ответить на callback query: {e}")

        if data == "status:view":
            await self.send_control(context, self.build_status_text())
        elif data == "search:now":
            await self.on_search_now(update, context)
        elif data == "tgsearch:now":
            await self._start_manual_telegram_search(context)
        elif data == "search:confirm:force":
            await self._start_manual_search(context, force=True)
        elif data == "search:cancel":
            await self.send_control(context, "❌ Ручной запуск отменён")
        elif data.startswith("schedule:apply:"):
            await self._apply_pending_schedule(update, context, data.split(":", 2)[2])
        elif data == "schedule:cancel":
            self._pending_schedule_cfg = None
            self._pending_schedule_input = False
            await self.send_control(context, "❌ Изменение расписания отменено")
        elif data == "schedule:view":
            await self.on_schedule_view(update, context)
        elif data == "schedule:set":
            await self.on_schedule_set(update, context)
        elif data == "schedule:toggle":
            await self.on_schedule_toggle(update, context)
        else:
            await self.send_control(context, f"Неизвестное действие: {data}")

    # ---------- Ручной поиск (/search и кнопка меню) ----------

    def _daily_limit_reached(self) -> bool:
        """True, если последний поиск был меньше суток назад"""
        info = self.runner.last_run_info() or {}
        last_run_str = info.get("last_run")
        if not last_run_str:
            return False
        try:
            from datetime import datetime, timedelta

            last_run = datetime.fromisoformat(str(last_run_str))
            return (datetime.now() - last_run) < timedelta(hours=24)
        except (ValueError, TypeError):
            return False

    async def cmd_search(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.warning(f"Неавторизованный /search от user {update.effective_user.id}")
            return
        args = list(context.args or [])
        force = bool(args) and args[0].lower() in ("force", "-f", "--force")

        if not force and self._daily_limit_reached():
            logger.info("Суточный лимит: запрашиваем подтверждение force-запуска")
            markup = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "⚠️ Всё равно искать (force)", callback_data="search:confirm:force"
                        )
                    ],
                    [InlineKeyboardButton("❌ Отмена", callback_data="search:cancel")],
                ]
            )
            await self.send_control(
                context,
                "⚠️ Последний поиск был меньше суток назад.\n"
                "Частые запуски могут привести к блокировке на hh.ru. Запустить принудительно?",
                reply_markup=markup,
            )
            return

        await self._start_manual_search(context, force=force)

    async def on_search_now(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Кнопка меню «Искать вакансии сейчас» — та же логика, что /search"""
        if self._daily_limit_reached():
            await self.cmd_search(update, context)
            return
        await self._start_manual_search(context, force=False)

    async def _start_manual_search(self, context: ContextTypes.DEFAULT_TYPE, force: bool) -> None:
        position = self.enqueue_manual_search(force=force)
        if position is None:
            await self.send_control(context, "⏳ Очередь переполнена, попробуйте позже")
        elif position == 1:
            await self.send_control(context, "🚀 Запускаю поиск…")
        else:
            await self.send_control(
                context,
                f"⏳ Задача в очереди (позиция {position}), " f"начнётся после завершения текущей",
            )

    def enqueue_manual_search(self, force: bool = False) -> Optional[int]:
        """
        Поставить manual_search-задачу в очередь.

        ProgressMessenger создаётся сразу, но активируется только при старте
        задачи (в coro_factory), чтобы статус не «висел», пока задача ждёт.
        """
        messenger = ProgressMessenger(
            bot=self._bot,
            chat_id=self.router.control()[0],
            thread_id=self.router.control()[1],
        )

        async def run_task() -> dict:
            await messenger.start("🚀 Поиск запущен")
            result = await self.runner.run_search(
                force=force,
                progress_cb=messenger.on_stage,
            )
            success = result.get("success_applies", 0)
            reason = result.get("stopped_reason") or "все вакансии обработаны"
            summary = f"✅ Поиск завершён: {success} откликов\nПричина остановки: {reason}"
            await messenger.finish(summary)
            return result

        task = Task(source=SOURCE_MANUAL_SEARCH, coro_factory=run_task)
        task.progress_cb = messenger
        task.on_done = self._on_manual_done
        return self.task_queue.enqueue(task)

    async def _on_manual_done(self, result) -> None:
        """Финальная краткая сводка ручного запуска в control-топик
        (детальный отчёт по-прежнему уходит в report-топик через JobApplier)"""
        if isinstance(result, Exception):
            logger.error(f"Ручной поиск упал: {result}")
            await self.send_control_direct(f"❌ Ошибка при выполнении поиска: {result}")
            return
        success = result.get("success_applies", 0)
        reason = result.get("stopped_reason") or "все вакансии обработаны"
        await self.send_control_direct(
            f"✅ Поиск завершён: {success} откликов\nПричина остановки: {reason}"
        )

    # ---------- Поиск по публичным Telegram-каналам ----------

    async def cmd_tgsearch(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        await self._start_manual_telegram_search(context)

    async def _start_manual_telegram_search(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.telegram_search_runner is None:
            await self.send_control(context, "⚠️ Telegram-поиск не настроен: добавьте data_folder/sources.yaml")
            return
        task = Task(
            source=SOURCE_MANUAL_TELEGRAM_SEARCH,
            coro_factory=self.telegram_search_runner.run_search,
            dedupe_key="telegram_search",
            on_done=self._on_telegram_search_done,
        )
        position = self.task_queue.enqueue(task)
        if position is None:
            await self.send_control(context, "⏳ Telegram-поиск уже выполняется или очередь переполнена")
        elif position == 1:
            await self.send_control(context, "🚀 Проверяю публичные Telegram-каналы…")
        else:
            await self.send_control(context, f"⏳ Telegram-поиск в очереди: {position}")

    async def _on_telegram_search_done(self, result) -> None:
        if isinstance(result, Exception):
            await self.send_control_direct(f"❌ Ошибка Telegram-поиска: {result}")
            return
        await self.send_control_direct(
            f"✅ Telegram-поиск завершён: проверено {result.get('checked', 0)}, "
            f"отправлено в «Отклики» {result.get('sent', 0)}"
        )

    # ---------- Меню управления расписанием ----------

    async def on_schedule_view(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.store is None:
            await self.send_control(context, "Расписание недоступно")
            return
        cfg = self.store.load()
        status = "включён" if cfg.enabled else "выключен"
        lines = ["⏰ Текущее расписание", f"Автозапуск: {status}"]
        lines.append(f"Cron: {cfg.cron}")
        lines.append(f"Часовой пояс: {cfg.timezone}")
        times = self.scheduler.next_fire_times(3) if self.scheduler and cfg.enabled else []
        if times:
            formatted = "\n".join(f"  • {t.strftime('%d.%m.%Y %H:%M %Z')}" for t in times)
            lines.append(f"Следующие запуски:\n{formatted}")
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("✏️ Изменить", callback_data="schedule:set")],
                [
                    InlineKeyboardButton(
                        "🔴 Выключить автозапуск" if cfg.enabled else "🟢 Включить автозапуск",
                        callback_data="schedule:toggle",
                    )
                ],
            ]
        )
        chat_id, thread_id = self.router.control()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text="\n".join(lines),
                reply_markup=markup,
            )
        except TelegramError as e:
            logger.error(f"Не удалось отправить расписание: {e}")

    async def on_schedule_set(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if self.store is None:
            await self.send_control(context, "Расписание недоступно")
            return
        self._pending_schedule_input = True
        self._pending_schedule_cfg = None
        await self.send_control(
            context,
            "✏️ Пришлите новое расписание одним сообщением:\n"
            "• cron-выражение: 0 9 * * *\n"
            "• cron + часовой пояс: 30 8 * * * Europe/Moscow\n"
            "• или просто время: 09:30\n\n"
            "Для отмены — /cancel",
        )

    async def _on_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """FSM-lite: ловим текст только в режиме ожидания ввода расписания"""
        if not self._authorized(update) or not self._pending_schedule_input:
            return
        text = update.effective_message.text or ""
        ok, result = self.store.validate_user_input(text)
        if not ok:
            await self.send_control(context, f"⚠️ {result}\nПопробуйте ещё раз или /cancel")
            return
        self._pending_schedule_input = False
        self._pending_schedule_cfg = result

        from src.application.schedule_store import describe_next_runs

        runs = describe_next_runs(result, 3)
        runs_text = "\n".join(f"  • {r.strftime('%d.%m.%Y %H:%M %Z')}" for r in runs)
        markup = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "💾 Сохранить", callback_data=f"schedule:apply:{self._cfg_hash(result)}"
                    )
                ],
                [InlineKeyboardButton("❌ Отмена", callback_data="schedule:cancel")],
            ]
        )
        preview = (
            f"📋 Превью нового расписания:\n"
            f"Cron: {result.cron}\n"
            f"Часовой пояс: {result.timezone}\n"
            f"Следующие запуски:\n{runs_text}\n\n"
            f"Сохранить?"
        )
        chat_id, thread_id = self.router.control()
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                message_thread_id=thread_id,
                text=preview,
                reply_markup=markup,
            )
        except TelegramError as e:
            logger.error(f"Не удалось отправить превью расписания: {e}")

    async def _apply_pending_schedule(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, cfg_hash: str
    ) -> None:
        """Кнопка «💾 Сохранить»: записать файл и применить расписание к шедулеру"""
        cfg = self._pending_schedule_cfg
        if cfg is None or self._cfg_hash(cfg) != cfg_hash:
            await self.send_control(
                context, "⚠️ Предложенное расписание устарело. Начните заново: /menu"
            )
            return
        self.store.save(cfg)
        if self.scheduler is not None:
            self.scheduler.apply_schedule(cfg)
        self._pending_schedule_cfg = None
        logger.info(f"Расписание обновлено из Telegram: {cfg.model_dump()}")
        await self.send_control(
            context,
            f"✅ Расписание сохранено и применено:\nCron: {cfg.cron}, TZ: {cfg.timezone}",
        )

    async def on_schedule_toggle(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Вкл/выкл автозапуска: влияет только на будущие триггеры; уже стоящие
        в очереди scheduled-задачи не отзываются (дедупликация не даст добавить новые)"""
        if self.store is None:
            await self.send_control(context, "Расписание недоступно")
            return
        cfg = self.store.load()
        cfg.enabled = not cfg.enabled
        self.store.save(cfg)
        if self.scheduler is not None:
            self.scheduler.apply_schedule(cfg)
        state = "включён 🟢" if cfg.enabled else "выключен 🔴"
        await self.send_control(context, f"Автозапуск {state}")

    async def cmd_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            return
        self._pending_schedule_input = False
        self._pending_schedule_cfg = None
        await self.send_control(context, "Действие отменено")

    # ---------- Генерация письма (/letter) ----------

    async def cmd_letter(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._authorized(update):
            logger.warning(f"Неавторизованный /letter от user {update.effective_user.id}")
            return
        if self.cover_letter_service is None:
            await self.send_control(context, "⚠️ Сервис писем недоступен")
            return
        text = " ".join(list(context.args or [])).strip()
        if not text:
            await self.send_control(
                context,
                "Пришлите ссылку на вакансию hh.ru или текст вакансии:\n" "/letter <ссылка|текст>",
            )
            return

        position = self.enqueue_letter_task(text)
        if position is None:
            await self.send_control(context, "⏳ Очередь переполнена, попробуйте позже")
        elif position == 1:
            await self.send_control(context, "🚀 Генерирую письмо…")
        else:
            await self.send_control(context, f"⏳ В очереди: {position}")

    def enqueue_letter_task(self, text: str) -> Optional[int]:
        """letter-задача выполняется тем же worker'ом очереди, что и поиск —
        гонка за единственный PlaywrightJobManager исключена архитектурно"""

        async def run_task() -> dict:
            letter = await self.cover_letter_service.generate(text)
            return {"letter": letter}

        task = Task(source="letter", coro_factory=run_task)
        task.on_done = self._on_letter_done
        return self.task_queue.enqueue(task)

    async def _on_letter_done(self, result) -> None:
        """Результат письма — только в control-топик (без дубля в jobs-топик)"""
        if isinstance(result, Exception):
            logger.error(f"Генерация письма упала: {result}")
            await self.send_control_direct(f"❌ Ошибка при генерации письма: {result}")
            return
        letter = result.get("letter", "")
        # длинное письмо режем на части по 4096 символов
        for i in range(0, len(letter), 4096):
            await self.send_control_direct(letter[i : i + 4096])

    # ---------- Сборка PTB-приложения ----------

    def register_handlers(self, app: Application) -> None:
        from telegram.ext import MessageHandler, filters

        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("menu", self.cmd_menu))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("search", self.cmd_search))
        app.add_handler(CommandHandler("tgsearch", self.cmd_tgsearch))
        app.add_handler(CommandHandler("letter", self.cmd_letter))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        # FSM-lite: текстовые сообщения для ввода расписания
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self._on_text))

    def create_app(self) -> Application:
        """Собрать PTB Application с зарегистрированными хендлерами (без запуска polling)"""
        # отдельные пулы соединений для API и для getUpdates
        request = build_ptb_request(self.secrets)
        get_updates_request = build_ptb_request(self.secrets)
        builder = (
            Application.builder()
            .token(self.secrets["tg_token"])
            .request(request)
            .get_updates_request(get_updates_request)
        )
        app = builder.build()
        self.register_handlers(app)
        self._app = app
        return app

    @classmethod
    def from_secrets_file(cls, secrets_file: str, **components) -> "HhApplierBot":
        secrets = load_yaml_file(secrets_file)
        router = TopicRouter(secrets)
        return cls(secrets=secrets, router=router, **components)
