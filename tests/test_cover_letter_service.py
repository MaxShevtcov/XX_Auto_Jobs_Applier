import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from src.llm.cover_letter_service import CoverLetterService


def make_service(mocker, tmp_path, resume_file=None):
    """Сервис с замоканным менеджером и GPTAnswerer"""
    # не писать резюме в реальный data_folder во время тестов
    mocker.patch("src.job_manager.resume_scraper.ResumeScraper.save_resume_info")

    manager = MagicMock()
    manager.get_my_resumes_from_browser = AsyncMock(
        return_value={"items": [{"id": "r1", "title": "Python dev"}]}
    )
    manager.get_resume_content_from_browser = AsyncMock(
        return_value={
            "personal_information": {
                "first_name": "Иван",
                "sex": "Мужской",
                "email": "ivan@test.ru",
            },
            "skills": ["Python"],
            "experience": "",
            "about_me": "",
            "educations": [],
        }
    )

    gpt = MagicMock()
    gpt.parse_contacts.return_value = {}
    gpt.write_cover_letter.return_value = "Здравствуйте! Я Аристаний, пишу вам про вакансию."
    mocker.patch("src.llm.cover_letter_service.GPTAnswerer", return_value=gpt)

    service = CoverLetterService(
        llm_api_key="key",
        llm_proxy=["p1"],
        manager_factory=AsyncMock(return_value=manager),
        resume_cache_path=str(tmp_path / "resume_fallback.yaml"),
    )
    return service, manager, gpt


@pytest.mark.asyncio
class TestCoverLetterService:
    async def test_generate_from_url(self, mocker, tmp_path):
        service, manager, gpt = make_service(mocker, tmp_path)
        mocker.patch(
            "src.llm.cover_letter_service.extract_vacancy",
            new=AsyncMock(return_value={"job_title": "Dev", "description": "d"}),
        )
        letter = await service.generate("hh.ru/vacancy/123")
        assert "вакансию" in letter
        gpt.set_job.assert_called_once()

    async def test_ttl_cache_one_scrape_for_two_calls(self, mocker, tmp_path):
        service, manager, _ = make_service(mocker, tmp_path)
        await service._ensure_resume()
        await service._ensure_resume()
        assert manager.get_resume_content_from_browser.await_count == 1

    async def test_refresh_after_ttl(self, mocker, tmp_path):
        service, manager, _ = make_service(mocker, tmp_path)
        service.ttl_sec = 0.05
        await service._ensure_resume()
        await asyncio.sleep(0.06)
        await service._ensure_resume()
        assert manager.get_resume_content_from_browser.await_count == 2

    async def test_generate_deanonymizes_output(self, mocker, tmp_path):
        """Персональные данные деанонимизируются в финальном письме"""
        service, _, gpt = make_service(mocker, tmp_path)
        # write_cover_letter вернёт письмо с dummy-данными после анонимизации резюме;
        # deanonymize должен вернуть реальное имя из personal_information
        letter = await service.generate("Текст вакансии без ссылки")
        assert "Иван" in letter or "Аристаний" not in letter

    async def test_fallback_to_cache_file_when_browser_down(self, mocker, tmp_path):
        fallback = tmp_path / "resume_fallback.yaml"
        fallback.write_text(
            yaml.safe_dump(
                {
                    "personal_information": {"first_name": "Иван", "sex": "Мужской"},
                    "skills": "Python",
                    "experience": "",
                }
            ),
            encoding="utf-8",
        )
        manager = MagicMock()
        manager.get_my_resumes_from_browser = AsyncMock(side_effect=RuntimeError("browser down"))
        gpt = MagicMock()
        gpt.write_cover_letter.return_value = "Письмо"
        mocker.patch("src.llm.cover_letter_service.GPTAnswerer", return_value=gpt)

        service = CoverLetterService(
            llm_api_key="k",
            llm_proxy=[],
            manager_factory=AsyncMock(return_value=manager),
            resume_cache_path=str(fallback),
        )
        letter = await service.generate("Вакансия")
        assert letter == "Письмо"

    async def test_llm_error_raises_readable_exception(self, mocker, tmp_path):
        service, _, gpt = make_service(mocker, tmp_path)
        gpt.write_cover_letter.side_effect = RuntimeError("LLM недоступна")

        with pytest.raises(Exception, match="LLM"):
            await service.generate("Вакансия")
