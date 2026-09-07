from typing import Any, Callable, Coroutine, List, Optional

from src.job_manager.bot_facade import BotFacade
from src.job_manager.job_applier import JobApplier
from src.job_manager.playwright_manager import PlaywrightJobManager
from src.job_manager.resume_scraper import ResumeScraper
from src.job_manager.search_customizer import SearchCustomizer
from src.llm.llm_manager import GPTAnswerer
from src.logger_config import logger

ProgressCallback = Callable[[str, str], Optional[Coroutine[Any, Any, None]]]


async def _notify(progress_cb: Optional[ProgressCallback], stage: str, detail: str) -> None:
    """Дёрнуть progress-callback, проглотив все ошибки (прогресс не должен ломать пайплайн)"""
    if not progress_cb:
        return
    try:
        result = progress_cb(stage, detail)
        if hasattr(result, "__await__"):
            await result
    except Exception as e:
        logger.error(f"Ошибка в progress callback на этапе {stage}: {e}")


async def run_search_pipeline(
    secrets: dict,
    parameters: dict,
    llm_api_key: str,
    llm_proxy: List[str],
    progress_cb: Optional[ProgressCallback] = None,
    force: bool = False,
    manager: Optional[PlaywrightJobManager] = None,
    fallback_api_key: Optional[str] = None,
) -> dict:
    """
    Оркестрация полного цикла поиска и отклика на вакансии.

    Если `manager` передан — не создавать и не закрывать свой менеджер браузера
    (владелец — long-running приложение). Иначе создать и закрыть как раньше.

    Возвращает {"success_applies": int, "stopped_reason": str}.
    """
    if secrets.get("hh_login") and secrets.get("hh_password"):
        parameters["hh_login"] = secrets["hh_login"]
        parameters["hh_password"] = secrets["hh_password"]

    job_title = parameters.get("job_title")

    own_manager = False
    if manager is None:
        manager = PlaywrightJobManager(secrets)
        await _notify(progress_cb, "login", "Инициализация браузера и вход в hh.ru")
        await manager.initialize()
        own_manager = True

    try:
        gpt_answerer_component = GPTAnswerer(
            llm_api_key,
            llm_proxy,
            fallback_api_key=fallback_api_key or secrets.get("llm_fallback_api_key"),
        )
        resume_component = ResumeScraper(
            manager, job_title, parameters.get("resume_id"), gpt_answerer_component
        )
        search_component = SearchCustomizer(manager)
        apply_component = JobApplier(
            manager, resume_component, search_component, update_schedule=not force
        )

        bot = BotFacade(resume_component, search_component, apply_component)

        await _notify(progress_cb, "parameters", "Получение параметров поиска")
        await bot.set_parameters(parameters)

        if not force and not apply_component.check_the_last_search_time():
            logger.warning("Последний поиск был меньше суток назад, завершаем работу")
            return {
                "success_applies": apply_component.success_applies_num,
                "stopped_reason": "Суточный лимит: последний поиск был меньше суток назад",
            }

        await _notify(progress_cb, "resume", "Сбор информации о резюме")
        await bot.set_resume()

        bot.set_search_parameters(parameters)
        bot.set_gpt_answerer(gpt_answerer_component, parameters)

        await _notify(progress_cb, "search", "Поиск вакансий начат")
        await bot.start_apply()

        success = apply_component.success_applies_num
        if success >= apply_component.max_applies_num:
            stopped_reason = f"Достигнуто максимальное число откликов за запуск: {success}"
        else:
            stopped_reason = ""
        return {"success_applies": success, "stopped_reason": stopped_reason}
    finally:
        if own_manager:
            await manager.close()
