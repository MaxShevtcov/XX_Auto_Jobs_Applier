import asyncio

import pytest

from src.application.task_queue import Task, TaskQueue


def make_task(source="manual_search", name="task", started=None, finished=None, delay=0):
    """Создать задачу с корутиной-заглушкой"""

    async def run():
        if started is not None:
            started.append(name)
        if delay:
            await asyncio.sleep(delay)
        if finished is not None:
            finished.append(name)
        return {"name": name}

    return Task(source=source, coro_factory=run)


@pytest.mark.asyncio
class TestTaskQueue:
    async def test_tasks_execute_sequentially_in_order(self):
        started, finished = [], []
        q = TaskQueue()
        await q.start()
        try:
            q.enqueue(make_task("manual_search", "a", started, finished, delay=0.02))
            q.enqueue(make_task("letter", "b", started, finished))
            q.enqueue(make_task("scheduled", "c", started, finished))
            for _ in range(100):
                if len(finished) == 3:
                    break
                await asyncio.sleep(0.01)
        finally:
            await q.stop()

        assert started == ["a", "b", "c"]
        assert finished == ["a", "b", "c"]

    async def test_enqueue_returns_position(self):
        q = TaskQueue()
        assert q.enqueue(make_task()) is None  # воркер ещё не запущен
        await q.start()
        try:
            t1 = make_task(delay=0.05)
            pos1 = q.enqueue(t1)
            assert pos1 == 1
            for _ in range(200):
                if q.is_busy:
                    break
                await asyncio.sleep(0.005)
            t2 = make_task()
            pos2 = q.enqueue(t2)
            assert pos2 == 2
            # позиция ожидающей задачи отслеживается по id
            assert q.position_of(t2.id) == 2
            assert q.position_of(t1.id) is None  # уже выполняется
        finally:
            await q.stop()

    async def test_scheduled_deduplication(self):
        q = TaskQueue()
        await q.start()
        try:
            slow = make_task(source="manual_search", delay=0.05)
            q.enqueue(slow)
            await asyncio.sleep(0.01)

            s1 = make_task(source="scheduled")
            m1 = make_task(source="manual_search")
            s2 = make_task(source="scheduled")

            assert q.enqueue(s1) is not None
            assert q.enqueue(m1) is not None
            assert q.enqueue(s2) is None  # дубликат scheduled отклонён
        finally:
            await q.stop()

    async def test_manual_not_deduplicated(self):
        q = TaskQueue()
        await q.start()
        try:
            q.enqueue(make_task(source="manual_search", delay=0.05))
            await asyncio.sleep(0.01)
            assert q.enqueue(make_task(source="manual_search")) is not None
            assert q.enqueue(make_task(source="manual_search")) is not None
        finally:
            await q.stop()

    async def test_queue_overflow_rejects(self):
        q = TaskQueue(maxsize=2)
        await q.start()
        try:
            blocker = make_task(delay=0.08)
            q.enqueue(blocker)
            await asyncio.sleep(0.01)
            assert q.enqueue(make_task()) is not None
            assert q.enqueue(make_task()) is not None
            assert q.enqueue(make_task()) is None  # переполнение
            # scheduled при переполнении тоже отклоняется без исключения
            assert q.enqueue(make_task(source="scheduled")) is None
        finally:
            await q.stop()

    async def test_worker_survives_task_exception(self):
        finished = []

        async def boom():
            raise RuntimeError("boom")

        q = TaskQueue()
        await q.start()
        try:
            q.enqueue(Task(source="manual_search", coro_factory=boom))
            q.enqueue(
                Task(
                    source="manual_search",
                    coro_factory=lambda: _ok(finished),
                )
            )
            for _ in range(100):
                if finished:
                    break
                await asyncio.sleep(0.01)
        finally:
            await q.stop()

        assert finished == ["ok"]

    async def test_on_done_called_with_result_and_exception(self):
        results = []

        async def collect(value):
            results.append(value)

        async def ok():
            return {"n": 42}

        async def fail():
            raise ValueError("x")

        q = TaskQueue()
        await q.start()
        try:
            q.enqueue(Task(source="manual_search", coro_factory=ok, on_done=collect))
            q.enqueue(Task(source="manual_search", coro_factory=fail, on_done=collect))
            for _ in range(100):
                if len(results) == 2:
                    break
                await asyncio.sleep(0.01)
        finally:
            await q.stop()

        assert results[0] == {"n": 42}
        assert isinstance(results[1], ValueError)

    async def test_is_busy_and_current_tracking(self):
        q = TaskQueue()
        await q.start()
        assert not q.is_busy
        assert q.current() is None

        started = []
        task = make_task(name="busy", started=started, delay=0.05)
        q.enqueue(task)
        for _ in range(100):
            if q.is_busy:
                break
            await asyncio.sleep(0.005)

        assert q.is_busy
        assert q.current() is task
        assert q.size() >= 1

        for _ in range(200):
            if not q.is_busy:
                break
            await asyncio.sleep(0.005)
        assert not q.is_busy
        assert q.current() is None


async def _ok(finished):
    finished.append("ok")
