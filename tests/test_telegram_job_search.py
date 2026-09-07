from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.telegram.job_search import PublicPost, TelegramJobSearchRunner, _PreviewParser


def test_preview_parser_extracts_message_text_time_and_links():
    parser = _PreviewParser("Test source", "test_channel")
    parser.feed(
        '''<div class="tgme_widget_message" data-post="test_channel/42">
        <div class="tgme_widget_message_text">Вакансия Python Developer
        <a href="https://t.me/hr_person">@hr_person</a>
        <a href="https://example.org/apply">Отклик</a>
        </div><time datetime="2026-09-07T10:00:00+00:00"></time></div>'''
    )

    assert len(parser.posts) == 1
    post = parser.posts[0]
    assert post.message_id == 42
    assert "Python Developer" in post.text
    assert post.published_at == "2026-09-07T10:00:00+00:00"
    assert "https://example.org/apply" in post.links


@pytest.mark.asyncio
async def test_runner_sends_matching_vacancy_with_real_contact(tmp_path, mocker):
    sources = tmp_path / "sources.yaml"
    sources.write_text(
        yaml.safe_dump(
            {
                "telegram_search": {"max_results_per_run": 20, "request_delay_seconds": 1},
                "sources": [{"name": "Test", "username": "@test_channel"}],
            }
        ),
        encoding="utf-8",
    )
    service = MagicMock()
    service.score_and_generate_job = AsyncMock(return_value=({"score": 85}, "Письмо"))
    sender = MagicMock()
    sender.send_job_description = AsyncMock()
    mocker.patch("src.telegram.job_search.TelegramReportSender", return_value=sender)
    runner = TelegramJobSearchRunner(
        {"job_title": "Python developer"}, service, str(sources), state_path=str(tmp_path / "state.yaml")
    )
    post = PublicPost(
        source="Test",
        username="test_channel",
        message_id=10,
        text="Вакансия Python Developer. Пишите @real_hr или заполните https://company.test/apply",
        published_at="2026-09-07T10:00:00+00:00",
        links=[],
    )
    runner._fetch_source = AsyncMock(return_value=[post])

    result = await runner.run_search()

    assert result["sent"] == 1
    description = sender.send_job_description.await_args.args[0]
    assert description.contacts == ["@real_hr"]
    assert description.application_links == ["https://company.test/apply"]
    assert description.link == "https://t.me/test_channel/10"


@pytest.mark.asyncio
async def test_runner_skips_post_without_any_application_path(tmp_path, mocker):
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump({"sources": [{"username": "@test_channel"}]}), encoding="utf-8")
    service = MagicMock()
    service.score_and_generate_job = AsyncMock()
    sender = MagicMock()
    sender.send_job_description = AsyncMock()
    mocker.patch("src.telegram.job_search.TelegramReportSender", return_value=sender)
    runner = TelegramJobSearchRunner({}, service, str(sources), state_path=str(tmp_path / "state.yaml"))
    runner._fetch_source = AsyncMock(
        return_value=[
            PublicPost("Test", "test_channel", 11, "Вакансия Backend developer", "", [])
        ]
    )

    result = await runner.run_search()

    assert result["sent"] == 0
    service.score_and_generate_job.assert_not_awaited()
    sender.send_job_description.assert_not_awaited()
