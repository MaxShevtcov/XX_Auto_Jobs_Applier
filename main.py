import asyncio
import traceback
from pathlib import Path
from typing import List

from src.constants import SEARCH_CONFIG_FILE, SEARCH_CONFIG_FILE_TMP, SECRETS_FILE
from src.job_manager.pipeline_runner import run_search_pipeline
from src.logger_config import logger
from src.utils.utils import load_yaml_file
from src.views.config import SearchConfig, Secrets

# TODO: create tests for json_to_readable, browser_utils
# TODO: actualize tests


class ConfigError(Exception):
    pass


class ConfigValidator:
    """Класс для проверки правильности настроек конфигурации"""

    def validate_search_config(
        self, config_yaml_path: Path, config_yaml_path_tmp: Path, secrets: dict
    ) -> dict:
        """Проверить правильность настроек из файла конфигурации поиска"""
        parameters = load_yaml_file(config_yaml_path)

        try:
            # Преобразование параметров в нужные типы
            for key, value in parameters.items():
                if value == "None" or value == "":
                    if key == "job_blacklist":
                        parameters[key] = []
                    else:
                        parameters[key] = None

            # Валидация параметров с помощью Pydantic
            config = SearchConfig(**parameters)
            logger.debug("Проверка параметров завершена успешно.")
            return config.model_dump()

        except Exception as e:
            raise ConfigError(f"Ошибка валидации конфигурации: {str(e)}")

    @staticmethod
    def validate_secrets(secrets_yaml_path: Path) -> dict:
        """Проверить наличие секретных ключей для LLM API"""
        secrets = load_yaml_file(secrets_yaml_path)

        try:
            secrets_config = Secrets(**secrets)
            return secrets_config.model_dump()
        except Exception as e:
            raise ConfigError(f"Ошибка валидации секретов: {str(e)}")


class FileManager:
    """Класс для поиска и проверки содержимого файла в папке данных"""

    @staticmethod
    def validate_data_folder(app_data_folder: Path) -> None:
        """Проверить наличие всех необходимых файлов настроек"""
        if not app_data_folder.exists() or not app_data_folder.is_dir():
            raise FileNotFoundError(f"Папка данных не найдена: {app_data_folder}")

        required_files = ["secrets.yaml", "search_config.yaml"]
        missing_files = [
            file for file in required_files if not (app_data_folder / file[:-5] / file).exists()
        ]

        if missing_files:
            raise FileNotFoundError(f"Отсутствуют файлы в папке данных: {', '.join(missing_files)}")

        output_folder = app_data_folder / "output"
        output_folder.mkdir(exist_ok=True)


async def create_and_run_bot(
    secrets: dict, parameters: dict, llm_api_key: str, llm_proxy: List[str]
):
    """Запустить бот"""
    await run_search_pipeline(secrets, parameters, llm_api_key, llm_proxy)


async def main() -> None:
    try:
        data_folder = Path("data_folder")
        FileManager.validate_data_folder(data_folder)

        config_validator = ConfigValidator()
        secrets = config_validator.validate_secrets(SECRETS_FILE)
        parameters = config_validator.validate_search_config(
            SEARCH_CONFIG_FILE, SEARCH_CONFIG_FILE_TMP, secrets
        )

        llm_api_key = secrets["llm_api_key"]
        llm_proxy = secrets["llm_proxy"]

        await create_and_run_bot(secrets, parameters, llm_api_key, llm_proxy)

        # Ждем в сумме 1 час перед следующим запуском
        # await asyncio.sleep(3000) # blocking wait in async? better use asyncio.sleep

    except ConfigError as ce:
        logger.error(f"Ошибка конфигурации: {str(ce)}")
    except FileNotFoundError as fnf:
        logger.error(f"Файл не найден: {str(fnf)}")
    except RuntimeError:
        tb_str = traceback.format_exc()
        logger.error(f"Runtime error\n{tb_str}")
    except Exception:
        tb_str = traceback.format_exc()
        logger.error(f"Неизвестная ошибка\n{tb_str}")
    finally:
        # time.sleep(600)
        pass


if __name__ == "__main__":
    asyncio.run(main())
