import asyncio
import time
from typing import Any, Callable, Coroutine, List, Optional

from src.job_manager.resume_scraper import ResumeScraper
from src.job_manager.vacancy_extractor import extract_vacancy
from src.llm.llm_manager import GPTAnswerer
from src.logger_config import logger
from src.utils.json_to_readable import transform_resume_data
from src.utils.utils import load_yaml_file

DEFAULT_RESUME_TTL_SEC = 6 * 3600  # 6 часов
DEFAULT_RESUME_CACHE_PATH = "data_folder/output/resume.yaml"


class CoverLetterService:
    """
    Генерация сопроводительных писем по ссылке или тексту вакансии.

    Резюме скрейпится Playwright'ом и кэшируется с TTL (по умолчанию 6 ч);
    при недоступном браузере — fallback на data_folder/output/resume.yaml.
    Персональные данные анонимизируются до LLM и деанонимизируются в ответе
    (существующий механизм ResumeScraper).
    """

    def __init__(
        self,
        llm_api_key: str,
        llm_proxy: List[str],
        manager_factory: Callable[[], Coroutine[Any, Any, Any]],
        ttl_sec: float = DEFAULT_RESUME_TTL_SEC,
        resume_cache_path: str = DEFAULT_RESUME_CACHE_PATH,
    ):
        self.llm_api_key = llm_api_key
        self.llm_proxy = llm_proxy
        self.manager_factory = manager_factory
        self.ttl_sec = ttl_sec
        self.resume_cache_path = resume_cache_path
        self._resume_scraper: Optional[ResumeScraper] = None
        self._cached_at: float = 0.0

    async def _get_manager(self):
        return await self.manager_factory()

    def _build_gpt_answerer(self) -> GPTAnswerer:
        return GPTAnswerer(self.llm_api_key, self.llm_proxy)

    async def _ensure_resume(self) -> ResumeScraper:
        """Резюме с TTL-кэшем; fallback на yaml-файл при недоступном браузере"""
        fresh = (time.monotonic() - self._cached_at) < self.ttl_sec
        if self._resume_scraper is not None and fresh:
            return self._resume_scraper
        try:
            manager = await self._get_manager()
            gpt = self._build_gpt_answerer()
            scraper = ResumeScraper(manager, "", "", gpt)
            resume_id, _titles = await scraper.get_resume_parameters()
            scraper.resume_id = resume_id
            await scraper.get_resume_info()
            self._resume_scraper = scraper
            self._cached_at = time.monotonic()
            logger.info("Резюме обновлено из браузера")
            return scraper
        except Exception as e:
            logger.warning(f"Не удалось получить резюме из браузера ({e}), fallback на файл")
            return self._load_resume_from_file()

    def _load_resume_from_file(self) -> ResumeScraper:
        """Fallback-резюме из кэш-файла (уже анонимизировано ранее)"""
        gpt = self._build_gpt_answerer()
        scraper = ResumeScraper.__new__(ResumeScraper)
        # минимальная инициализация без менеджера
        scraper.manager = None
        scraper.job_title = ""
        scraper.resume_id = ""
        scraper.github_links = []
        scraper.gpt_answerer_component = gpt
        try:
            resume_info = load_yaml_file(self.resume_cache_path) or {}
        except Exception as e:
            raise RuntimeError(
                f"Резюме недоступно: браузер не отвечает, кэш-файл не читается ({e})"
            )
        scraper.resume_info = resume_info
        scraper.personal_information = dict(resume_info.get("personal_information", {}))
        readable = transform_resume_data(resume_info)
        scraper._readable = readable
        self._resume_scraper = scraper
        self._cached_at = time.monotonic()
        return scraper

    async def generate(self, text: str) -> str:
        """
        Полный цикл генерации письма:
        extract → set_resume → set_job → write_cover_letter → deanonymize.

        Вызывается ТОЛЬКО из worker'а TaskQueue.
        """
        scraper = await self._ensure_resume()

        manager = getattr(scraper, "manager", None)
        if manager is None:
            manager = await self._get_manager()
        job = await extract_vacancy(text, manager)

        gpt = self._build_gpt_answerer()
        readable = getattr(scraper, "_readable", None)
        if readable is None:
            from src.utils.json_to_readable import transform_resume_data as trd

            readable = trd(scraper.resume_info)
        gpt.set_resume(scraper.resume_info, readable)
        gpt.set_job(job)

        # блокирующий LLM-вызов не должен замораживать event loop бота
        letter = await asyncio.to_thread(gpt.write_cover_letter)
        letter = scraper.deanonymize_personal_information(letter)
        logger.info("Сопроводительное письмо сгенерировано для /letter")
        return letter

    async def score_job(self, job: dict, parameters: dict) -> tuple[dict, Any, str]:
        """Вернуть оценку и контекст резюме для вакансии без генерации письма."""
        scraper = await self._ensure_resume()
        readable = getattr(scraper, "_readable", None) or transform_resume_data(scraper.resume_info)
        gpt = self._build_gpt_answerer()
        gpt.set_resume(scraper.resume_info, readable)
        gpt.set_search_parameters(parameters)
        gpt.set_job(job)
        return await asyncio.to_thread(gpt.job_is_interesting), scraper, readable

    async def score_and_generate_job(
        self, job: dict, parameters: dict, threshold: int = 70
    ) -> tuple[dict, Optional[str]]:
        """Оценить уже извлечённую вакансию и подготовить письмо.

        Используется Telegram-поиском: текст вакансии уже получен из публичного
        поста, поэтому Playwright для самой вакансии не нужен.
        """
        score_data, scraper, readable = await self.score_job(job, parameters)
        if int(score_data.get("score", 0) or 0) < threshold:
            return score_data, None
        gpt = self._build_gpt_answerer()
        gpt.set_resume(scraper.resume_info, readable)
        gpt.set_job(job)
        letter = await asyncio.to_thread(gpt.write_cover_letter)
        return score_data, scraper.deanonymize_personal_information(letter)
