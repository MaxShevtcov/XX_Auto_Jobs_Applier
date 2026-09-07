import asyncio
import itertools
from dataclasses import dataclass, field
from typing import Callable, Coroutine, Optional

from src.logger_config import logger

SOURCE_SCHEDULED = "scheduled"
SOURCE_MANUAL_SEARCH = "manual_search"
SOURCE_LETTER = "letter"
SOURCE_TELEGRAM_SEARCH = "telegram_search"
SOURCE_MANUAL_TELEGRAM_SEARCH = "manual_telegram_search"


@dataclass
class Task:
    """Задача для последовательной очереди"""

    source: str  # "scheduled" | "manual_search" | "letter"
    coro_factory: Callable[[], Coroutine]
    progress_cb: Optional[Callable] = None
    on_done: Optional[Callable] = None  # async или sync: (result | Exception) -> None
    dedupe_key: Optional[str] = None
    id: int = field(default_factory=itertools.count(1).__next__)


class TaskQueue:
    """
    Последовательная очередь браузероёмких задач.

    Задачи выполняются строго по одной (браузер один, конкурентность недопустима).
    Дедупликация scheduled-задач; ограничение размера очереди.
    """

    def __init__(self, maxsize: int = 10):
        self._maxsize = maxsize
        self._queue: asyncio.Queue = asyncio.Queue()
        self._pending: list[Task] = []
        self._current: Optional[Task] = None
        self._worker: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Запускает worker-корутину"""
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._worker_loop())
            logger.info("TaskQueue worker запущен")

    async def stop(self) -> None:
        """Останавливает worker: текущая задача дожидается, ожидающие остаются в очереди"""
        if self._worker is None:
            return
        while self.is_busy:
            await asyncio.sleep(0.01)
        worker, self._worker = self._worker, None
        worker.cancel()
        try:
            await worker
        except asyncio.CancelledError:
            pass
        logger.info("TaskQueue worker остановлен")

    def enqueue(self, task: Task) -> Optional[int]:
        """
        Ставит задачу в очередь. Возвращает позицию (1 = выполняется сейчас).

        None — если очередь переполнена, воркер не запущен
        или это дубликат scheduled-задачи.
        """
        if self._worker is None or self._worker.done():
            logger.error("Постановка задачи в незапущенную очередь")
            return None
        if self._is_duplicate(task):
            logger.warning("Задача уже в очереди, дубликат отклонён")
            return None
        if len(self._pending) >= self._maxsize:
            logger.warning(f"Очередь переполнена ({self._maxsize}), задача отклонена")
            return None
        self._pending.append(task)
        self._queue.put_nowait(task)
        position = self.position_of(task.id)
        logger.info(f"Задача source={task.source} поставлена в очередь (позиция {position})")
        return position

    def _is_duplicate(self, task: Task) -> bool:
        """Дедупликация scheduled-задач и задач с явно заданным ключом."""
        key = task.dedupe_key
        if key is None and task.source == SOURCE_SCHEDULED:
            key = SOURCE_SCHEDULED
        if key is None:
            return False
        def task_key(item: Task) -> Optional[str]:
            return item.dedupe_key or (SOURCE_SCHEDULED if item.source == SOURCE_SCHEDULED else None)
        return any(task_key(item) == key for item in self._pending) or (
            self._current is not None and task_key(self._current) == key
        )

    async def _worker_loop(self) -> None:
        while True:
            task = await self._queue.get()
            try:
                if task in self._pending:
                    self._pending.remove(task)
                self._current = task
                logger.info(f"Начало выполнения задачи source={task.source} id={task.id}")
                result = await task.coro_factory()
                logger.info(f"Задача source={task.source} id={task.id} завершена успешно")
                if task.on_done:
                    await _maybe_await(task.on_done(result))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                tb = __import__("traceback").format_exc()
                logger.error(f"Задача source={task.source} упала с ошибкой: {e}\n{tb}")
                if task.on_done:
                    try:
                        await _maybe_await(task.on_done(e))
                    except Exception:
                        tb2 = __import__("traceback").format_exc()
                        logger.error(f"Ошибка в on_done задачи source={task.source}:\n{tb2}")
            finally:
                self._current = None
                self._queue.task_done()

    @property
    def is_busy(self) -> bool:
        return self._current is not None

    def current(self) -> Optional[Task]:
        return self._current

    def position_of(self, task_id) -> Optional[int]:
        """
        Позиция ожидающей задачи с учётом текущей выполняющейся:
        1 = начнётся сейчас (очередь пуста), 2 = после завершения текущей и т.д.
        """
        for i, t in enumerate(self._pending):
            if t.id == task_id:
                return i + (2 if self._current is not None else 1)
        return None

    def size(self) -> int:
        """Сколько задач в очереди (ожидающие + текущая)"""
        return len(self._pending) + (1 if self._current else 0)


async def _maybe_await(result) -> None:
    if hasattr(result, "__await__"):
        await result
