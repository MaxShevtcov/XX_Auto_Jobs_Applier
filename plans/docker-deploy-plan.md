# Plan: Запуск через Docker + Docker Compose + Cron на Ubuntu-сервере

**Created:** 2026-05-08
**Status:** Ready for Atlas Execution

---

## Summary

Приложение XX_Auto_Jobs_Applier запускается как однократный скрипт (`python main.py`), использует
Playwright (Chromium headless), Telegram-бот, LLM API и сохраняет состояние в `data_folder/`.
Встроенного планировщика нет — только проверка `check_the_last_search_time()`, которая пропускает
запуск, если он уже был менее 24 ч назад.  
Цель: упаковать в Docker-образ, запускать через Docker Compose, добавить cron (хостовый или
supercronic-контейнер) для периодического запуска.

---

## Context & Analysis

**Relevant Files:**
- `main.py` — точка входа, запускается через `asyncio.run(main())`, без CLI-аргументов
- `src/utils/browser_utils.py` — конфигурация Playwright; `--no-sandbox` уже присутствует; `HEADLESS_MODE` из `app_config.yaml`
- `src/constants.py` — все пути относительные (`data_folder/...`, `logs/`)
- `data_folder/app_config/app_config.yaml` — `HEADLESS_MODE: false` → **нужно изменить на `true` для Docker**
- `data_folder/secrets/secrets.yaml` — HH-логин/пароль, LLM API key, Telegram token и chat IDs
- `data_folder/browser_session/hh_state.json` — Playwright session state (персистентный)
- `data_folder/output/` — результаты (письма, yaml-отчёты)
- `logs/` — loguru-логи
- `requirements.txt` — зависимости Python

**Key Functions/Classes:**
- `check_the_last_search_time()` в `JobApplier` — пропускает запуск при повторе < 24 ч
- `create_playwright_browser()` в `browser_utils.py` — запускает Chromium с `--no-sandbox`
- `HEADLESS_MODE` из `app_config.yaml` — **нужно `true` в серверном окружении**

**Dependencies:**
- `playwright` — требует Chromium + системные библиотеки (libglib, libnss, libatk и др.)
- `python-telegram-bot`, `telethon` — Telegram API
- `langchain*`, `openai`, `langchain-google-genai` — LLM-интеграции
- Python 3.12 (указан в `pyproject.toml` target-version)

**Patterns & Conventions:**
- Все пути относительные от CWD → рабочая директория внутри контейнера должна быть `/app`
- Конфиги и секреты — YAML-файлы, не env-переменные → монтируются как volumes
- Приложение проверяет дату последнего запуска → безопасно запускать cron чаще 1 раза в 24 ч

---

## Implementation Phases

### Phase 1: Dockerfile

**Objective:** Создать Dockerfile на базе официального Playwright-образа с Python 3.12.

**Files to Create:**
- `Dockerfile`

**Шаги:**

1. Базовый образ — `mcr.microsoft.com/playwright/python:v1.51.0-jammy`  
   Выбор обоснован: содержит Chromium со всеми системными зависимостями, Python 3.11+ (в jammy — 3.10, но можно добавить deadsnakes PPA или использовать образ с Python 3.12).  
   **Альтернатива:** `python:3.12-slim` + ручная установка Playwright-зависимостей (`playwright install-deps chromium`).  
   **Рекомендация:** `python:3.12-slim` + `RUN pip install playwright && playwright install chromium && playwright install-deps chromium` — чище по версии Python.

2. Установить системные утилиты для Playwright (`playwright install-deps`).

3. Скопировать `requirements.txt`, установить зависимости (кешируемый слой).

4. Скопировать исходный код (без `data_folder/`, `logs/`, `virtual/`).

5. Установить рабочую директорию `/app`.

6. Добавить непривилегированного пользователя `appuser` для запуска без root.

7. Команда по умолчанию — `python main.py`.

**Acceptance Criteria:**
- [ ] `docker build` проходит без ошибок
- [ ] `docker run --rm <image> python -c "from playwright.async_api import async_playwright; print('OK')"` выводит OK
- [ ] Образ не запускается от root

---

### Phase 2: .dockerignore

**Objective:** Исключить ненужные файлы из контекста сборки.

**Files to Create:**
- `.dockerignore`

**Содержимое:**
```
virtual/
.git/
__pycache__/
*.pyc
*.pyo
logs/
data_folder/
*.md
.pytest_cache/
tests/
notebooks/
```

> `data_folder/` исключается из образа — монтируется как volume.  
> `logs/` — тоже volume.

**Acceptance Criteria:**
- [ ] `docker build` не копирует `data_folder/` внутрь образа
- [ ] Размер контекста сборки < 5 MB

---

### Phase 3: docker-compose.yml

**Objective:** Описать сервис, volumes и restart-политику для продакшн-запуска.

**Files to Create:**
- `docker-compose.yml`

**Описание сервиса:**

```yaml
version: "3.9"

services:
  hh-applier:
    build: .
    image: hh-applier:latest
    container_name: hh_applier
    working_dir: /app
    volumes:
      - ./data_folder:/app/data_folder      # конфиги, секреты, сессия, output
      - ./logs:/app/logs                    # персистентные логи
    restart: "no"                           # однократный запуск (cron управляет повторами)
    # Для запуска через cron без постоянного демона.
    # Если нужен демон — поменять на: restart: unless-stopped + добавить внутренний scheduler.
    
    # Необязательные настройки безопасности (seccomp для Chromium):
    # security_opt:
    #   - seccomp:unconfined    # если --no-sandbox недостаточно
```

**Acceptance Criteria:**
- [ ] `docker compose run --rm hh-applier` запускает приложение и завершается
- [ ] `./data_folder/output/` заполняется файлами результатов
- [ ] `./logs/` содержит `app.log` и `error.log`

---

### Phase 4: Настройка HEADLESS_MODE для Docker

**Objective:** Убедиться, что Chromium запускается headless в контейнере.

**Files to Modify:**
- `data_folder/app_config/app_config.yaml` — изменить `HEADLESS_MODE: false` → `HEADLESS_MODE: true`

> **Важно:** Без этого Playwright попытается открыть GUI-браузер и упадёт с ошибкой  
> `playwright._impl._errors.Error: Browser closed unexpectedly`.

**Acceptance Criteria:**
- [ ] `HEADLESS_MODE: true` в `app_config.yaml`
- [ ] Playwright успешно запускает Chromium в Docker без GUI

---

### Phase 5: Cron для периодического запуска (хостовый)

**Objective:** Добавить cron-задание на хосте Ubuntu для запуска контейнера каждые 4–6 часов.

**Стратегия:** Хостовый cron вызывает `docker compose run --rm hh-applier`.  
Это безопаснее и проще, чем cron внутри контейнера:
- нет постоянно работающего контейнера
- логи и session state сохраняются через volumes
- встроенная проверка `check_the_last_search_time()` предотвращает дублирование откликов

**Files to Create:**
- `scripts/run_with_cron.sh` — wrapper-скрипт для cron

**Содержимое скрипта:**
```bash
#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="$PROJECT_DIR/logs/cron.log"

mkdir -p "$PROJECT_DIR/logs"

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting hh-applier" >> "$LOG_FILE"
cd "$PROJECT_DIR"
docker compose run --rm hh-applier >> "$LOG_FILE" 2>&1
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finished" >> "$LOG_FILE"
```

**Установка cron (выполняется вручную на сервере):**
```bash
chmod +x scripts/run_with_cron.sh
crontab -e
```

Добавить строку (каждые 6 часов):
```cron
0 */6 * * * /home/maxim/dev/XX_Auto_Jobs_Applier/scripts/run_with_cron.sh
```

> Или через `systemd timer` (альтернатива, надёжнее cron на Ubuntu):
> ```
> # /etc/systemd/system/hh-applier.service
> # /etc/systemd/system/hh-applier.timer
> ```
> Детальное описание — в Phase 6 (опционально).

**Acceptance Criteria:**
- [ ] `scripts/run_with_cron.sh` запускает контейнер и пишет лог
- [ ] Cron-задание установлено (`crontab -l` показывает запись)
- [ ] `logs/cron.log` появляется после первого срабатывания

---

### Phase 6 (Опционально): systemd timer как альтернатива cron

**Objective:** Более надёжная альтернатива cron для Ubuntu.

**Files to Create:**
- `scripts/hh-applier.service`
- `scripts/hh-applier.timer`

**hh-applier.service:**
```ini
[Unit]
Description=HH Auto Jobs Applier (Docker)
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
WorkingDirectory=/home/maxim/dev/XX_Auto_Jobs_Applier
ExecStart=/usr/bin/docker compose run --rm hh-applier
StandardOutput=append:/home/maxim/dev/XX_Auto_Jobs_Applier/logs/cron.log
StandardError=append:/home/maxim/dev/XX_Auto_Jobs_Applier/logs/cron.log
```

**hh-applier.timer:**
```ini
[Unit]
Description=Run HH Auto Jobs Applier every 6 hours
Requires=hh-applier.service

[Timer]
OnBootSec=5min
OnUnitActiveSec=6h
Persistent=true

[Install]
WantedBy=timers.target
```

**Установка:**
```bash
sudo cp scripts/hh-applier.service /etc/systemd/system/
sudo cp scripts/hh-applier.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hh-applier.timer
```

**Acceptance Criteria:**
- [ ] `systemctl status hh-applier.timer` показывает активный timer
- [ ] `journalctl -u hh-applier.service` показывает логи запуска

---

## Open Questions

1. **Версия Python в образе?**
   - **Option A:** `mcr.microsoft.com/playwright/python:v1.51.0-jammy` — Python 3.10 (старше целевой 3.12), но все Playwright-зависимости уже включены.
   - **Option B:** `python:3.12-slim` + ручной `playwright install chromium && playwright install-deps chromium` (~500 MB зависимостей).
   - **Recommendation:** Option B (`python:3.12-slim`) — точнее соответствует целевой версии проекта; `playwright install-deps` установит всё необходимое за один шаг.

2. **Cron или systemd timer?**
   - **Option A:** Хостовый cron — универсален, прост.
   - **Option B:** systemd timer — надёжнее, поддерживает `Persistent=true` (запустит пропущенный запуск после ребута), лучше интегрирован с Ubuntu.
   - **Recommendation:** systemd timer для продакшн Ubuntu-сервера.

3. **Нужен ли `.env` файл?**
   - Текущий код читает секреты из `secrets.yaml`, не из env.
   - В Docker лучшей практикой являются env-переменные или Docker Secrets.
   - Для минимальных изменений кода: оставить `secrets.yaml` как volume-mount.
   - Рефакторинг на env-переменные — отдельная задача, не входит в этот план.

---

## Risks & Mitigation

- **Risk:** Playwright падает в Docker с `--no-sandbox` но всё равно не может запустить Chromium.
  - **Mitigation:** Добавить `security_opt: - seccomp:unconfined` в docker-compose.yml; или запускать контейнер от root (не рекомендуется).

- **Risk:** Сессия HH (`hh_state.json`) протухает, бот не логинится.
  - **Mitigation:** Первый запуск необходимо выполнить вручную в headful-режиме (`HEADLESS_MODE: false`) на хосте для получения session state, затем переключить на headless и перенести `hh_state.json` на сервер.

- **Risk:** Telegram не может отправить captcha-скриншот из headless-контейнера.
  - **Mitigation:** Captcha-изображение сохраняется в `data_folder/` и отправляется через Telegram API — это не требует GUI, работает в headless.

- **Risk:** Параллельные запуски cron если предыдущий ещё не завершился.
  - **Mitigation:** Использовать `flock` в wrapper-скрипте или `docker compose run` (каждый раз создаёт новый контейнер, не конфликтует с другим запуском).  
    Добавить в скрипт: `exec 200>/var/lock/hh-applier.lock; flock -n 200 || exit 0`.

- **Risk:** data_folder монтируется с неправильными правами.
  - **Mitigation:** Указать `user: "1000:1000"` в docker-compose.yml, соответствующий UID хостового пользователя.

---

## Success Criteria

- [ ] `docker build -t hh-applier .` завершается без ошибок
- [ ] `docker compose run --rm hh-applier` запускает бот, бот логинится на HH и выполняет поиск
- [ ] `data_folder/output/` содержит результаты после запуска
- [ ] `logs/app.log` содержит записи из контейнера
- [ ] Cron или systemd timer настроен и запускает контейнер по расписанию
- [ ] Повторный запуск в течение 24 ч пропускается благодаря `check_the_last_search_time()`

---

## Notes for Atlas

**Порядок реализации:**
1. Сначала Phase 2 (`.dockerignore`) и Phase 4 (изменение `HEADLESS_MODE`).
2. Затем Phase 1 (`Dockerfile`) и Phase 3 (`docker-compose.yml`).
3. Проверить сборку и тестовый запуск.
4. Добавить Phase 5 (cron-скрипт) и опционально Phase 6 (systemd).

**Dockerfile (готовый к использованию):**
```dockerfile
FROM python:3.12-slim

WORKDIR /app

# Системные зависимости для Playwright Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python-зависимости (кешируемый слой)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Установка Playwright Chromium + системных зависимостей
RUN playwright install chromium && playwright install-deps chromium

# Создать непривилегированного пользователя
RUN useradd -m -u 1000 appuser

# Копировать исходный код
COPY src/ ./src/
COPY main.py .

# Создать директории для volumes (права для appuser)
RUN mkdir -p data_folder logs && chown -R appuser:appuser /app

USER appuser

CMD ["python", "main.py"]
```

**docker-compose.yml (готовый к использованию):**
```yaml
version: "3.9"

services:
  hh-applier:
    build: .
    image: hh-applier:latest
    container_name: hh_applier
    working_dir: /app
    user: "1000:1000"
    volumes:
      - ./data_folder:/app/data_folder
      - ./logs:/app/logs
    restart: "no"
```

**Важно для первого запуска на сервере:**
1. Скопировать `data_folder/` с рабочей сессией (`hh_state.json`) с локальной машины на сервер.
2. Убедиться, что `HEADLESS_MODE: true` в `data_folder/app_config/app_config.yaml`.
3. Запустить `docker compose run --rm hh-applier` вручную и убедиться, что лог выглядит корректно.
4. Настроить cron/systemd timer.
