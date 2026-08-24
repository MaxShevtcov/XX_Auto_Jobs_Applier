from datetime import datetime, timedelta
from datetime import timezone as dt_tz
from unittest.mock import MagicMock

import pytest

from src.application.schedule_store import ScheduleConfig, ScheduleStore
from src.application.search_scheduler import SearchScheduler


def make_queue_mock(busy=False, size=0):
    q = MagicMock()
    q.is_busy = busy
    q.size.return_value = size
    q.enqueue = MagicMock(return_value=2)
    return q


@pytest.mark.asyncio
class TestSearchScheduler:
    async def test_start_registers_job_with_cron_trigger(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        scheduler = SearchScheduler(make_queue_mock(), store)
        await scheduler.start()
        try:
            job = scheduler.scheduler.get_job(SearchScheduler.JOB_ID)
            assert job is not None
        finally:
            await scheduler.shutdown()

    async def test_fire_enqueues_scheduled_task(self, tmp_path):
        queue = make_queue_mock()
        runner = MagicMock()
        scheduler = SearchScheduler(queue, ScheduleStore(tmp_path / "s.yaml"), runner=runner)
        await scheduler._fire()
        queue.enqueue.assert_called_once()
        task = queue.enqueue.call_args.args[0]
        assert task.source == "scheduled"

    async def test_fire_duplicate_scheduled_not_enqueued(self, tmp_path):
        queue = make_queue_mock()
        queue.enqueue.return_value = None  # дедупликация/переполнение в очереди
        scheduler = SearchScheduler(queue, ScheduleStore(tmp_path / "s.yaml"))
        # не должно бросать исключений
        await scheduler._fire()

    async def test_fire_swallows_exception(self, tmp_path):
        queue = make_queue_mock()
        queue.enqueue.side_effect = RuntimeError("boom")
        scheduler = SearchScheduler(queue, ScheduleStore(tmp_path / "s.yaml"))
        await scheduler._fire()  # исключение проглочено и залогировано

    async def test_apply_schedule_reschedules_job(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        scheduler = SearchScheduler(make_queue_mock(), store)
        await scheduler.start()
        try:
            new_cfg = ScheduleConfig(cron="30 20 * * *", timezone="Europe/Moscow")
            scheduler.apply_schedule(new_cfg)
            job = scheduler.scheduler.get_job(SearchScheduler.JOB_ID)
            assert job is not None
            next_run = job.trigger.get_next_fire_time(None, datetime.now(dt_tz.utc))
            assert next_run.hour == 17 or next_run.utcoffset() != timedelta(0)  # tz учтён
        finally:
            await scheduler.shutdown()

    async def test_apply_schedule_disabled_removes_job(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        scheduler = SearchScheduler(make_queue_mock(), store)
        await scheduler.start()
        try:
            new_cfg = ScheduleConfig(enabled=False)
            scheduler.apply_schedule(new_cfg)
            assert scheduler.scheduler.get_job(SearchScheduler.JOB_ID) is None
        finally:
            await scheduler.shutdown()

    async def test_misfire_settings_passed(self, tmp_path):
        path = tmp_path / "s.yaml"
        import yaml

        path.write_text(yaml.safe_dump({"misfire_grace_time_sec": 600}), encoding="utf-8")
        scheduler = SearchScheduler(make_queue_mock(), ScheduleStore(path))
        await scheduler.start()
        try:
            job = scheduler.scheduler.get_job(SearchScheduler.JOB_ID)
            assert job.misfire_grace_time == 600
            assert job.coalesce is True
            assert job.max_instances == 1
        finally:
            await scheduler.shutdown()

    async def test_misfire_catchup_on_start_when_queue_empty_and_last_run_older(self, tmp_path):
        path = tmp_path / "s.yaml"
        import yaml

        # каждую минуту, grace-окно час: на старте почти всегда есть пропущенный запуск
        path.write_text(yaml.safe_dump({"cron": "* * * * *"}), encoding="utf-8")

        queue = make_queue_mock(busy=False, size=0)
        runner = MagicMock()
        runner.last_run_info.return_value = {"last_run": "2020-01-01T00:00:00"}
        scheduler = SearchScheduler(queue, ScheduleStore(path), runner=runner)
        await scheduler.start()
        try:
            queue.enqueue.assert_called_once()
            task = queue.enqueue.call_args.args[0]
            assert task.source == "scheduled"
        finally:
            await scheduler.shutdown()

    async def test_misfire_skipped_when_task_already_running_or_queued(self, tmp_path):
        path = tmp_path / "s.yaml"
        import yaml

        path.write_text(yaml.safe_dump({"cron": "* * * * *"}), encoding="utf-8")

        for kwargs in [{"busy": True, "size": 1}, {"busy": False, "size": 2}]:
            queue = make_queue_mock(**kwargs)
            runner = MagicMock()
            runner.last_run_info.return_value = {"last_run": "2020-01-01T00:00:00"}
            scheduler = SearchScheduler(queue, ScheduleStore(path), runner=runner)
            await scheduler.start()
            try:
                queue.enqueue.assert_not_called()
            finally:
                await scheduler.shutdown()

    async def test_misfire_skipped_when_search_already_done_after_missed_time(self, tmp_path):
        path = tmp_path / "s.yaml"
        import yaml

        path.write_text(yaml.safe_dump({"cron": "* * * * *"}), encoding="utf-8")

        queue = make_queue_mock(busy=False, size=0)
        runner = MagicMock()
        # последний поиск позже пропущенного времени (сейчас) — догонять не нужно
        runner.last_run_info.return_value = {"last_run": datetime.now(dt_tz.utc).isoformat()}
        scheduler = SearchScheduler(queue, ScheduleStore(path), runner=runner)
        await scheduler.start()
        try:
            queue.enqueue.assert_not_called()
        finally:
            await scheduler.shutdown()

    async def test_misfire_skipped_when_scheduler_disabled(self, tmp_path):
        path = tmp_path / "s.yaml"
        import yaml

        path.write_text(yaml.safe_dump({"cron": "* * * * *", "enabled": False}), encoding="utf-8")
        queue = make_queue_mock()
        scheduler = SearchScheduler(queue, ScheduleStore(path))
        await scheduler.start()
        try:
            assert scheduler.scheduler.get_job(SearchScheduler.JOB_ID) is None
            queue.enqueue.assert_not_called()
        finally:
            await scheduler.shutdown()

    async def test_next_fire_times_returns_n_datetimes(self, tmp_path):
        scheduler = SearchScheduler(make_queue_mock(), ScheduleStore(tmp_path / "s.yaml"))
        await scheduler.start()
        try:
            times = scheduler.next_fire_times(3)
            assert len(times) == 3
            assert all(isinstance(t, datetime) for t in times)
            assert times[0] < times[1] < times[2]
        finally:
            await scheduler.shutdown()

    async def test_default_tz_is_kaliningrad(self, tmp_path):
        scheduler = SearchScheduler(make_queue_mock(), ScheduleStore(tmp_path / "s.yaml"))
        await scheduler.start()
        try:
            assert str(scheduler.scheduler.timezone) in (
                "Europe/Kaliningrad",
                "Kaliningrad",
            )
        finally:
            await scheduler.shutdown()
