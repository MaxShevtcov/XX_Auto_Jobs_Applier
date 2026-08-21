---
name: tdd-workflow
description: Testing conventions for XX_Auto_Jobs_Applier — pytest with pytest-asyncio, pytest-mock and coverage. Use when writing or running tests in this repo.
---

## TDD-first approach (по умолчанию)

**Правило:** если пользователь не сказал явно иначе, разработка ведётся через TDD.

Цикл:
1. **Red** — написать тест под ожидаемое поведение (тест падает)
2. **Green** — написать минимальную реализацию, чтобы тест прошёл
3. **Refactor** — улучшить код, тесты остаются зелёными

Исключения (когда TDD не применяется):
- Пользователь явно сказал "без тестов" или "prototype/sketch"
- Срочный хотфикс

## Запуск тестов

Фреймворк: **pytest** (единственный, не unittest-скрипты вручную).

```bash
pytest                                  # все тесты (+coverage по src из pytest.ini)
pytest tests/test_llm_manager.py        # один файл
pytest tests/test_main.py::test_name    # один тест
```

- `pytest.ini`: `--cov=src --cov-report=term-missing`, testpaths=`tests`, pythonpath=`. src`
- Маркеры: `asyncio` (pytest-asyncio), `authenticator`
- DeprecationWarning подавляются через filterwarnings — не «чинить» их в коде ради тестов

## Структура тестов
- Тест-файлы: `tests/test_<module>.py` — зеркалируют модули `src/` (например `tests/test_playwright_manager.py` для `src/job_manager/playwright_manager.py`)
- Классы `Test*`, функции `test_*`

## Mocking
- Использовать фикстуру `mocker` из **pytest-mock** (`mocker.patch`, `mocker.AsyncMock`)
- LLM вызовы мокать на уровне `LlmManager` — никаких реальных обращений к OpenAI/Gemini API в тестах
- Playwright: мокать `page`/`browser` объекты (locator, wait_for_selector и т.д.), не запускать реальный браузер
- Telegram: мокать клиенты `python-telegram-bot` и `telethon` — без сети
- YAML конфиги в тестах — временные файлы или фикстуры с тестовыми данными, никогда реальные `data_folder/secrets/secrets.yaml`
- Сеть недоступна в unit-тестах: httpx вызовы мокать

## Шаблон теста
```python
import pytest

class TestJobApplier:
    def test_should_skip_blacklisted_company(self, mocker):
        mocker.patch("src.job_manager.job_applier.LlmManager")
        applier = JobApplier(...)
        result = applier.is_interesting(job_with_blacklisted_company)
        assert result is False
```
