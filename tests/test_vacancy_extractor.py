from unittest.mock import AsyncMock, MagicMock

import pytest

from src.job_manager.vacancy_extractor import VACANCY_URL_RE, extract_vacancy

FULL_INFO = {
    "title": "Python разработчик",
    "salary": "100000-200000 руб",
    "experience": "1–3 года",
    "employment": "Полная занятость",
    "hiring_formats": "",
    "schedule": "",
    "working_hours": "",
    "work_formats": "Удалённая работа",
    "description": "Описание вакансии",
    "skills": "Python, SQL",
}


class TestVacancyUrlRegex:
    def test_matches_https_url(self):
        assert VACANCY_URL_RE.search("https://hh.ru/vacancy/12345678").group(1) == "12345678"

    def test_matches_without_scheme(self):
        assert VACANCY_URL_RE.search("hh.ru/vacancy/999").group(1) == "999"

    def test_matches_with_utm_params(self):
        m = VACANCY_URL_RE.search("https://hh.ru/vacancy/555?utm_source=test&hhtmFrom=chat")
        assert m.group(1) == "555"

    def test_no_match_for_plain_text(self):
        assert VACANCY_URL_RE.search("Просто текст про работу") is None


@pytest.mark.asyncio
class TestExtractVacancy:
    async def test_from_url_uses_playwright_scrape(self, mocker):
        manager = MagicMock()
        manager.get_vacancy_full_info = AsyncMock(return_value=dict(FULL_INFO))

        job = await extract_vacancy("Смотри https://hh.ru/vacancy/12345678", manager)

        manager.get_vacancy_full_info.assert_awaited_once_with("https://hh.ru/vacancy/12345678")
        assert job["job_title"] == "Python разработчик"
        assert job["vacancy_id"] == "12345678"
        assert job["description"] == "Описание вакансии"

    async def test_job_is_valid_model_dump(self, mocker):
        manager = MagicMock()
        manager.get_vacancy_full_info = AsyncMock(return_value=dict(FULL_INFO))
        job = await extract_vacancy("hh.ru/vacancy/42", manager)
        assert isinstance(job, dict)
        assert "description" in job and "salary" in job

    async def test_plain_text_becomes_description(self):
        job = await extract_vacancy("Ищем питониста в крутую команду", None)
        assert job["job_title"] == "Вакансия из чата"
        assert "питониста" in job["description"]

    async def test_long_text_truncated(self):
        long_text = "x" * 30000
        job = await extract_vacancy(long_text, None)
        assert len(job["description"]) <= 15000

    async def test_url_scrape_failure_falls_back_to_text(self):
        manager = MagicMock()
        manager.get_vacancy_full_info = AsyncMock(side_effect=RuntimeError("page dead"))
        text = "https://hh.ru/vacancy/1 и немного контекста"
        job = await extract_vacancy(text, manager)
        # фолбэк: сырой текст как описание
        assert job["job_title"] == "Вакансия из чата"
        assert "немного контекста" in job["description"]
