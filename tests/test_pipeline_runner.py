import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.job_manager.pipeline_runner import run_search_pipeline


@pytest.fixture
def fake_components(mocker):
    """Моки всех компонентов пайплайна"""
    manager = MagicMock()
    manager.initialize = AsyncMock()
    manager.close = AsyncMock()

    gpt_answerer = MagicMock()
    mocker.patch("src.job_manager.pipeline_runner.GPTAnswerer", return_value=gpt_answerer)

    resume_component = MagicMock()
    resume_component.get_resume_parameters = AsyncMock(return_value=("rid1", ["Title"]))
    resume_component.get_resume_info = AsyncMock(
        return_value=({"personal_information": {}}, "readable")
    )
    mocker.patch("src.job_manager.pipeline_runner.ResumeScraper", return_value=resume_component)

    search_component = MagicMock()
    mocker.patch("src.job_manager.pipeline_runner.SearchCustomizer", return_value=search_component)

    apply_component = MagicMock()
    apply_component.check_the_last_search_time.return_value = True
    apply_component.start_applying = AsyncMock()
    apply_component.success_applies_num = 0
    apply_component.max_applies_num = 100
    mocker.patch("src.job_manager.pipeline_runner.JobApplier", return_value=apply_component)

    bot_facade = MagicMock()
    bot_facade.set_parameters = AsyncMock()
    bot_facade.set_resume = AsyncMock()
    bot_facade.set_gpt_answerer = MagicMock()
    bot_facade.start_apply = AsyncMock()
    mocker.patch("src.job_manager.pipeline_runner.BotFacade", return_value=bot_facade)

    return {
        "manager": manager,
        "gpt_answerer": gpt_answerer,
        "resume_component": resume_component,
        "search_component": search_component,
        "apply_component": apply_component,
        "bot_facade": bot_facade,
    }


SECRETS = {
    "hh_login": "login",
    "hh_password": "password",
}
PARAMETERS = {"job_title": "Python dev"}


class TestRunSearchPipeline:
    @pytest.mark.asyncio
    async def test_orchestrates_components_in_order(self, fake_components):
        order = []

        facade = fake_components["bot_facade"]
        facade.set_parameters.side_effect = lambda *a: order.append(
            "set_parameters"
        ) or asyncio.sleep(0)
        facade.set_resume.side_effect = lambda *a: order.append("set_resume") or asyncio.sleep(0)
        facade.set_gpt_answerer.side_effect = lambda *a: order.append(
            "set_gpt_answerer"
        ) or asyncio.sleep(0)
        facade.start_apply.side_effect = lambda *a: order.append("start_apply") or asyncio.sleep(0)

        result = await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [])

        assert order == ["set_parameters", "set_resume", "set_gpt_answerer", "start_apply"]
        assert result == {"success_applies": 0, "stopped_reason": ""}
        fake_components["bot_facade"].start_apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_external_manager_not_closed(self, fake_components):
        manager = fake_components["manager"]
        result = await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [], manager=manager)
        manager.initialize.assert_not_awaited()
        manager.close.assert_not_awaited()
        assert result["success_applies"] >= 0

    @pytest.mark.asyncio
    async def test_own_manager_created_and_closed(self, fake_components, mocker):
        manager = fake_components["manager"]
        mocker.patch("src.job_manager.pipeline_runner.PlaywrightJobManager", return_value=manager)
        await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [])
        manager.initialize.assert_awaited_once()
        manager.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_force_bypasses_last_search_check(self, fake_components):
        apply_component = fake_components["apply_component"]
        apply_component.check_the_last_search_time.return_value = False
        facade = fake_components["bot_facade"]

        # без force — поиск не должен начаться
        result1 = await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [])
        assert result1["stopped_reason"] != ""
        facade.start_apply.assert_not_awaited()

        # c force — должен начаться
        await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [], force=True)
        facade.start_apply.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_progress_cb_called_on_stages(self, fake_components):
        stages = []

        async def cb(stage: str, detail: str) -> None:
            stages.append(stage)

        await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [], progress_cb=cb)
        assert "login" in stages
        assert "resume" in stages
        assert "search" in stages

    @pytest.mark.asyncio
    async def test_progress_cb_error_is_swallowed(self, fake_components):
        async def bad_cb(stage: str, detail: str) -> None:
            raise RuntimeError("telegram is down")

        result = await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [], progress_cb=bad_cb)
        assert result is not None

    @pytest.mark.asyncio
    async def test_exception_closes_own_manager_and_reraises(self, fake_components, mocker):
        manager = fake_components["manager"]
        mocker.patch("src.job_manager.pipeline_runner.PlaywrightJobManager", return_value=manager)
        facade = fake_components["bot_facade"]
        facade.start_apply = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError, match="boom"):
            await run_search_pipeline(SECRETS, dict(PARAMETERS), "key", [])
        manager.close.assert_awaited_once()
