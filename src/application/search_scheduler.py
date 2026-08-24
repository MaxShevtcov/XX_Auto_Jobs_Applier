from datetime import datetime, timedelta
from datetime import timezone as dt_tz
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from src.application.schedule_store import ScheduleConfig, ScheduleStore
from src.application.task_queue import SOURCE_SCHEDULED, Task, TaskQueue
from src.logger_config import logger


class SearchScheduler:
    """
    Шедулер ежедневного автопоиска на APScheduler (AsyncIOScheduler + CronTrigger).

    Ставит scheduled-задачу в единую TaskQueue; дедупликация и переполнение
    обрабатываются самой очередью. Пропущенные запуски догоняются политикой
    misfire_grace_time + coalesce с защитой от задвоения.
    """

    JOB_ID = "daily_search"

    def __init__(
        self,
        task_queue: TaskQueue,
        store: ScheduleStore,
        runner=None,
        errors_notifier=None,
    ):
        """
        :param task_queue: единая очередь задач
        :param store: хранилище расписания
        :param runner: SearchRunner для запуска поиска и чтения last_run
        :param errors_notifier: async callable(str) — короткое сообщение в err-топик
        """
        self.task_queue = task_queue
        self.store = store
        self.runner = runner
        self.errors_notifier = errors_notifier
        self.scheduler: Optional[AsyncIOScheduler] = None

    async def start(self) -> None:
        cfg = self.store.load()
        self.scheduler = AsyncIOScheduler(timezone=cfg.timezone)
        if cfg.enabled:
            self._add_job(cfg)
        self.scheduler.start()
        logger.info(f"Шедулер запущен: cron='{cfg.cron}' tz={cfg.timezone} enabled={cfg.enabled}")
        if cfg.enabled:
            self._maybe_catchup_missed_run(cfg)

    async def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            logger.info("Шедулер остановлен")
            self.scheduler = None

    def _add_job(self, cfg: ScheduleConfig) -> None:
        trigger = CronTrigger.from_crontab(cfg.cron, timezone=cfg.timezone)
        self.scheduler.add_job(
            self._fire,
            trigger=trigger,
            id=self.JOB_ID,
            misfire_grace_time=cfg.misfire_grace_time_sec,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )

    def apply_schedule(self, cfg: ScheduleConfig) -> None:
        """Пересоздать job при изменении расписания из бота"""
        if self.scheduler is None:
            logger.error("apply_schedule вызван до старта шедулера")
            return
        if not cfg.enabled:
            self.scheduler.remove_job(self.JOB_ID)
            logger.info("Автозапуск отключён, job удалён из шедулера")
            return
        # remove+add вместо reschedule_job — меняется и trigger, и grace-настройки
        existing = self.scheduler.get_job(self.JOB_ID)
        if existing is not None:
            self.scheduler.remove_job(self.JOB_ID)
        self._add_job(cfg)
        logger.info(f"Расписание обновлено: cron='{cfg.cron}' tz={cfg.timezone}")

    def _make_search_coro(self):
        return self.runner.run_search()

    async def _fire(self) -> None:
        """Триггер шедулера: поставить scheduled-задачу в очередь"""
        try:
            position = self.task_queue.enqueue(
                Task(source=SOURCE_SCHEDULED, coro_factory=self._make_search_coro)
            )
            if position is None:
                logger.warning("Scheduled-задача уже в очереди либо очередь переполнена")
            else:
                logger.info(f"Scheduled-поиск поставлен в очередь (позиция {position})")
        except Exception as e:
            tb = __import__("traceback").format_exc()
            logger.error(f"Ошибка в job шедулера: {e}\n{tb}")
            if self.errors_notifier:
                try:
                    await self.errors_notifier(f"⚠️ Ошибка шедулера: {e}")
                except Exception as send_error:
                    logger.error(f"Не удалось отправить ошибку в err-топик: {send_error}")

    def _missed_fire_time(self, cfg: ScheduleConfig) -> Optional[datetime]:
        """
        Время пропущенного запуска в пределах grace-окна, если оно есть.

        Ищем первое срабатывание после (now - grace - eps); если оно не позже now
        и внутри grace-окна — процесс его проспал.
        """
        trigger = CronTrigger.from_crontab(cfg.cron, timezone=cfg.timezone)
        now = datetime.now(dt_tz.utc)
        grace = timedelta(seconds=cfg.misfire_grace_time_sec)
        eps = timedelta(seconds=5)
        candidate = trigger.get_next_fire_time(None, now - grace - eps)
        if candidate is None:
            return None
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=dt_tz.utc)
        if (now - grace) <= candidate <= now:
            return candidate
        return None

    def _maybe_catchup_missed_run(self, cfg: ScheduleConfig) -> None:
        """
        Misfire-догон: ставим один scheduled-догоняющий запуск, только если
        очередь пуста, ничего не выполняется и последний успешный поиск был
        раньше планового времени пропущенного прогона.
        """
        try:
            missed = self._missed_fire_time(cfg)
            if missed is None:
                return
            if self.task_queue.is_busy or self.task_queue.size() > 0:
                logger.info("Misfire-догон пропущен: очередь занята")
                return
            last_run_info = self.runner.last_run_info() if self.runner is not None else {}
            last_run_str = last_run_info.get("last_run")
            if last_run_str:
                try:
                    last_run = datetime.fromisoformat(str(last_run_str))
                    if last_run.tzinfo is None:
                        last_run = last_run.replace(tzinfo=dt_tz.utc)
                    missed_aware = missed.astimezone(last_run.tzinfo)
                    if last_run >= missed_aware:
                        logger.info(
                            "Misfire-догон пропущен: поиск уже выполнен после пропущенного времени"
                        )
                        return
                except (ValueError, TypeError):
                    logger.warning("Некорректный last_run в кэше, догоняем пропуск")
            position = self.task_queue.enqueue(
                Task(source=SOURCE_SCHEDULED, coro_factory=self._make_search_coro)
            )
            if position is None:
                logger.warning("Misfire-догон отклонён очередью (дубликат/переполнение)")
            else:
                logger.info(
                    f"Misfire-догон пропущенного запуска {missed.isoformat()} "
                    f"поставлен в очередь (позиция {position})"
                )
        except Exception as e:
            tb = __import__("traceback").format_exc()
            logger.error(f"Ошибка при обработке misfire: {e}\n{tb}")

    def next_fire_times(self, n: int = 3) -> list[datetime]:
        """Следующие n времён запуска по расписанию (для превью в меню)"""
        if self.scheduler is None:
            return []
        job = self.scheduler.get_job(self.JOB_ID)
        if job is None:
            return []
        times = []
        fire_time = None
        now = datetime.now(job.trigger.timezone)
        for _ in range(n):
            fire_time = job.trigger.get_next_fire_time(fire_time, now)
            if fire_time is None:
                break
            times.append(fire_time)
            now = fire_time
        return times
