from typing import Any, Callable, Coroutine, List

from src import constants
from src.job_manager.pipeline_runner import run_search_pipeline
from src.logger_config import logger
from src.utils.utils import load_yaml_file


class SearchRunner:
    """
    Исполнитель браузероёмких задач (без своей конкурентности):
    вызывается ТОЛЬКО из worker'а TaskQueue.
    """

    def __init__(
        self,
        secrets: dict,
        parameters: dict,
        llm_api_key: str,
        llm_proxy: List[str],
        manager_factory: Callable[[], Coroutine[Any, Any, Any]],
    ):
        self.secrets = secrets
        self.parameters = parameters
        self.llm_api_key = llm_api_key
        self.llm_proxy = llm_proxy
        self.manager_factory = manager_factory
        self._manager = None

    async def _get_manager(self):
        """Ленивая инициализация менеджера браузера с пересозданием после падения"""
        if self._manager is None:
            logger.info("Создание нового PlaywrightJobManager")
            self._manager = await self.manager_factory()
        return self._manager

    async def get_manager(self):
        """Публичный доступ к общему менеджеру браузера (для CoverLetterService)"""
        return await self._get_manager()

    async def run_search(self, *, force: bool = False, progress_cb=None) -> dict:
        """
        Запустить полный цикл поиска и отклика на вакансии.
        Вызывается ТОЛЬКО из worker'а TaskQueue.
        """
        manager = await self._get_manager()
        try:
            return await run_search_pipeline(
                self.secrets,
                dict(self.parameters),
                self.llm_api_key,
                self.llm_proxy,
                progress_cb=progress_cb,
                force=force,
                manager=manager,
            )
        except Exception:
            # менеджер мог сломаться (краш браузера) — сбрасываем для пересоздания
            logger.warning("Поиск упал, сбрасываем менеджер браузера для пересоздания")
            broken, self._manager = self._manager, None
            if broken is not None:
                try:
                    await broken.close()
                except Exception as e:
                    logger.error(f"Ошибка при закрытии сломанного менеджера: {e}")
            raise

    def last_run_info(self) -> dict:
        """Чтение кэша последнего запуска (last_run, success_applies_num)"""
        try:
            cache = load_yaml_file(constants.LAST_RUN_FILE) or {}
            return {
                "last_run": cache.get("last_run"),
                "success_applies_num": cache.get("success_applies_num", 0),
            }
        except Exception:
            return {}
