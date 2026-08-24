from unittest.mock import AsyncMock

import pytest
import yaml

from src.application.search_runner import SearchRunner

SECRETS = {"hh_login": "l", "hh_password": "p"}
PARAMETERS = {"job_title": "dev"}


@pytest.fixture
def manager_factory():
    manager = AsyncMock()
    factory = AsyncMock(return_value=manager)
    return factory, manager


@pytest.mark.asyncio
class TestSearchRunner:
    async def test_run_search_uses_external_manager_and_pipeline(self, mocker, manager_factory):
        factory, manager = manager_factory
        pipeline_mock = mocker.patch(
            "src.application.search_runner.run_search_pipeline",
            new=AsyncMock(return_value={"success_applies": 3, "stopped_reason": "limit"}),
        )

        runner = SearchRunner(SECRETS, PARAMETERS, "key", ["p1"], manager_factory=factory)
        result = await runner.run_search(force=True)

        assert result == {"success_applies": 3, "stopped_reason": "limit"}
        pipeline_mock.assert_awaited_once()
        kwargs = pipeline_mock.await_args.kwargs
        assert kwargs["manager"] is manager
        assert kwargs["force"] is True

    async def test_manager_reused_between_runs(self, mocker, manager_factory):
        factory, manager = manager_factory
        mocker.patch(
            "src.application.search_runner.run_search_pipeline",
            new=AsyncMock(return_value={"success_applies": 0, "stopped_reason": ""}),
        )
        runner = SearchRunner(SECRETS, PARAMETERS, "key", [], manager_factory=factory)
        await runner.run_search()
        await runner.run_search()
        assert factory.await_count == 1

    async def test_broken_manager_recreated_on_next_run(self, mocker, manager_factory):
        factory, manager = manager_factory
        pipeline_mock = AsyncMock(
            side_effect=[
                RuntimeError("browser crashed"),
                {"success_applies": 1, "stopped_reason": ""},
            ]
        )
        mocker.patch("src.application.search_runner.run_search_pipeline", new=pipeline_mock)

        runner = SearchRunner(SECRETS, PARAMETERS, "key", [], manager_factory=factory)
        with pytest.raises(RuntimeError, match="browser crashed"):
            await runner.run_search()

        # после падения менеджер закрывается и пересоздаётся при следующем запуске
        runner2 = SearchRunner(SECRETS, PARAMETERS, "key", [], manager_factory=factory)
        result = await runner2.run_search()
        assert result["success_applies"] == 1

    async def test_last_run_info_reads_cache_file(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "last_run.yaml"
        cache_file.write_text(
            yaml.safe_dump({"last_run": "2026-08-24T09:00:00", "success_applies_num": 7}),
            encoding="utf-8",
        )
        monkeypatch.setattr("src.constants.LAST_RUN_FILE", str(cache_file))

        runner = SearchRunner(SECRETS, PARAMETERS, "key", [], manager_factory=AsyncMock())
        info = runner.last_run_info()
        assert info == {"last_run": "2026-08-24T09:00:00", "success_applies_num": 7}

    async def test_last_run_info_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.constants.LAST_RUN_FILE", str(tmp_path / "missing.yaml"))
        runner = SearchRunner(SECRETS, PARAMETERS, "key", [], manager_factory=AsyncMock())
        assert runner.last_run_info() == {}
