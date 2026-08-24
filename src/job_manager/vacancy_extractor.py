import re
from typing import Any, Dict, Optional

from src.job_manager.playwright_manager import PlaywrightJobManager
from src.logger_config import logger
from src.views.job import Job

VACANCY_URL_RE = re.compile(r"hh\.ru/vacancy/(\d+)", re.IGNORECASE)
MAX_TEXT_LENGTH = 15000


async def extract_vacancy(text: str, manager: Optional[PlaywrightJobManager]) -> Dict[str, Any]:
    """
    Извлечь вакансию из текста сообщения.

    Если найдена ссылка hh.ru/vacancy/<id> и браузер доступен — Playwright-скрейп
    страницы вакансии (HH API отложен до отдельной инициативы).
    Иначе — текст сообщения становится описанием вакансии (обрезка 15000 символов).
    """
    match = VACANCY_URL_RE.search(text or "")
    if match and manager is not None:
        vacancy_id = match.group(1)
        url = f"https://hh.ru/vacancy/{vacancy_id}"
        try:
            info = await manager.get_vacancy_full_info(url)
            if info:
                job = Job(**info)
                job.vacancy_id = vacancy_id
                if not job.job_title:
                    job.job_title = info.get("title") or "Вакансия из чата"
                logger.info(f"Вакансия получена по ссылке: {job.job_title}")
                return job.model_dump()
        except Exception as e:
            logger.warning(
                f"Не удалось скрейпить вакансию по ссылке ({e}), "
                f"используем сырой текст сообщения"
            )
    description = (text or "").strip()[:MAX_TEXT_LENGTH]
    return Job(job_title="Вакансия из чата", description=description).model_dump()
