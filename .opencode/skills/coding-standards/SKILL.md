---
name: coding-standards
description: Coding standards for XX_Auto_Jobs_Applier — Python 3.12, Playwright/Selenium browser automation of hh.ru, LangChain LLM calls, Telegram bots. Use when writing or reviewing Python code in this repo.
---

## Development workflow (TDD-first)
- **По умолчанию** разработка через TDD: сначала тест (red), потом реализация (green), потом рефакторинг
- Исключения: явный запрос "без тестов", срочный хотфикс, prototype
- После любого изменения кода: `pytest` (убедиться что тесты зелёные)
- После фикса бага: сначала написать тест, воспроизводящий баг, потом чинить

## Python / стиль
- Python 3.12, форматирование **black** (line-length 100), импорты **isort** (profile=black) — настройки в `pyproject.toml`
- Линтинг: **flake8** (`--extend-ignore=D,E,F403,F405,W503`)
- Всё через pre-commit: `pre-commit run --all-files` (trailing-whitespace, isort, black, flake8)
- Логирование через **loguru** (`src/logger_config.py`), никаких `print()` в src/
- Конфиги — PyYAML из `data_folder/`; загружать через `yaml.safe_load`, не `yaml.load`

## Структура проекта
- `main.py` — точка входа
- `src/job_manager/` — автоматизация hh.ru: `playwright_manager.py` (браузер), `job_applier.py` (отклики), `resume_scraper.py`, `search_customizer.py`, `bot_facade.py`
- `src/llm/` — работа с LLM: `llm_manager.py`, промпты в `prompts.py`, константы в `constants.py`, парсер логов API в `parse_llm_api_calls.py`
- `src/resume_builder/` — генерация резюме (промпты в `resume_prompt/`, стили в `resume_style/`)
- `src/telegram/` — `telegram_manager.py` (python-telegram-bot), `telegram_error_handler.py`, telethon для чтения капчи
- `src/utils/`, `src/views/`, `src/constants.py`

## Конфигурация
- `data_folder/secrets/secrets.yaml` — hh_login/hh_password, llm_api_key, llm_proxy, tg_token/tg_api_id/tg_api_hash. **Никогда не коммитить, не логировать, не отправлять в LLM**
- `data_folder/search_config/search_config.yaml` — параметры поиска вакансий
- `data_folder/app_config/app_config.yaml` — MONKEY_MODE, SEARCH_MODE, JOB_IS_INTERESTING_THRESH, RAISE_RESUME, MINIMUM_WAIT_TIME_SEC, MINIMUM_LOG_LEVEL, LLM_MODEL_TYPE, LLM_MODEL, TEMPERATURE
- Глобальные константы: `src/constants.py` и `src/llm/constants.py`

## LLM (LangChain)
- Провайдеры: `langchain-google-genai` (Gemini) и `langchain_openai` — все вызовы через `src/llm/llm_manager.py`
- Прокси для LLM API задаётся в `secrets.yaml` (`llm_proxy`, формат `логин:пароль@IP:порт`) — не логировать полный URL с кредами
- Персональные данные анонимизируются перед отправкой в LLM (`DUMMY_PERSONAL_INFO_MALE/FEMALE` в `src/constants.py`) — новые поля персональных данных тоже обязаны проходить анонимизацию
- Ответы LLM кэшируются в `data_folder/output/answers.yaml` — перед новым вызовом проверять кэш

## Браузерная автоматизация
- Основной путь — **Playwright** (`src/job_manager/playwright_manager.py`), Selenium — legacy
- Профиль Chrome хранится в `chrome_profile/` (куки сессии hh.ru) — не коммитить, не чистить без необходимости
- Всегда использовать явные ожидания (wait_for_selector / expect), не `time.sleep` где можно
- Капча: приложение сообщает в Telegram-топик Captcha и ждёт решение; селениумные грабли — см. раздел «Проблемы» README

## Telegram
- Исходящие сообщения (ошибки, отчёты, капча) — `python-telegram-bot` (`telegram_manager.py`)
- Чтение решений капчи из чата — `telethon` (async)
- ID чата/топиков задаются в secrets.yaml и прокидываются через `src/llm/constants.py`

## Docker
- `Dockerfile` + `docker-compose.yml`; при редактировании compose добавлять секцию `logging` с ротацией (max-size 10m, max-file 3)
- Перед перезаписью `docker-compose.yml` делать `.bak`-копию
