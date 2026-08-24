# Plan: Long-running контейнер с Telegram-ботом, встроенным шедулером поиска и генерацией сопроводительных писем

**Created:** 2026-08-24
**Status:** Ready for Atlas Execution
**Revision:** v4 — **ФИНАЛЬНАЯ** (все открытые вопросы закрыты пользователем; очередь задач вместо lock'а; дефолт TZ `Europe/Kaliningrad`; multi-user отклонён; вакансии по ссылке — только Playwright)

> **Что изменилось относительно v1:** убраны вариант отдельного docker-сервиса и любая зависимость от внешнего cron/systemd. Контейнер работает постоянно (`restart: unless-stopped`), ежедневный поиск триггерит встроенный шедулер (APScheduler) внутри того же процесса, что и Telegram-бот. Добавлены фазы для шедулера, персистентности расписания и меню управления им. **Остались валидными из v1:** Phase 1 (вынос `run_search_pipeline`), извлечение вакансии (`vacancy_extractor.py`), фича `/letter`, резюме через Playwright + TTL-кэш, механизм анонимизации.
>
> **Что изменилось относительно v2:** бот работает не в отдельном чате, а в существующем форум-чате `@xx_feedback` (`tg_chat_id` из секретов) с маршрутизацией сообщений по топикам через `message_thread_id`. Введён новый опциональный секрет `tg_control_topic_id` для интерактивного меню/команд; все новые ключи — опциональные с fallback'ами на существующие.
>
> **Что изменилось относительно v3:** все оставшиеся открытые вопросы закрыты. Ключевое архитектурное изменение: вместо «lock + отказ/пропуск при занятости» введена **последовательная очередь задач** (`asyncio.Queue` + worker) — scheduled-запуск при занятости ставится в очередь (не пропускается), `/letter` во время поиска тоже становится в очередь (не отклоняется). Дефолт часового пояса — `Europe/Kaliningrad`. Multi-user не нужен (один пользователь, allowlist). Вакансия по ссылке — только Playwright (HH API отложен). Результат `/letter` не дублируется в jobs-топик.

## Summary

Единый long-running Python-процесс объединяет: (1) Telegram-бота на python-telegram-bot с интерактивным меню — просмотр/изменение расписания автопоиска, ручной запуск `/search`, генерация писем `/letter`; (2) встроенный шедулер на **APScheduler** (`AsyncIOScheduler` + `CronTrigger`), который ежедневно запускает тот же `run_search_pipeline()`. Расписание персистится в `data_folder/schedule/schedule.yaml` (volume уже смонтирован), дефолт TZ — `Europe/Kaliningrad`. Все браузероёмкие задачи (scheduled/manual поиск, `/letter`) проходят через **единую последовательную очередь** (`TaskQueue`): задачи выполняются строго по одной, бот сообщает позицию в очереди. Пропущенные запуски догоняются политикой APScheduler (`misfire_grace_time` + `coalesce`) с защитой от задвоения. Основной сценарий разового запуска `main.py` сохраняется для локальной отладки.

## Context & Analysis

### Как запускается сейчас (исследование точки входа)

- **`main.py`** — разовый прогон: `asyncio.run(main())` → валидация конфигов → `create_and_run_bot()` → выход. Никакого внутреннего цикла (sleep-код закомментирован).
- **`Dockerfile`**: `CMD ["python", "main.py"]` — контейнер умирает после одного прогона.
- **`docker-compose.yml`**: `restart: "no"` — запуск предполагается извне (host cron / `docker compose run`), что противоречит новым требованиям и убирается этим планом.
- **Шедулера в requirements нет** — добавляем `APScheduler>=3.10,<4`.
- **Суточный лимит hh.ru** уже реализован внутри пайплайна: `JobApplier.check_the_last_search_time()` + кэш `LAST_RUN_FILE` (`data_folder/output/last_run.yaml`) — остаётся последней линией защиты при любых триггерах.

### Точки переиспользования (без изменений с v1)

| Что | Где | Как переиспользовать |
|---|---|---|
| Оркестрация пайплайна | `main.py::create_and_run_bot()` | Вынести в `src/job_manager/pipeline_runner.py::run_search_pipeline(...)` |
| Валидация конфигов | `main.py::ConfigValidator`; `src/views/config.py` (`SearchConfig`, `Secrets`) | Прямой импорт |
| Скрейпинг резюме | `ResumeScraper.get_resume_parameters()/get_resume_info()`; `PlaywrightJobManager.get_my_resumes_from_browser()` (:1385), `get_resume_content_from_browser()` (:1443) | Полное переиспользование |
| Анонимизация | `ResumeScraper.anonymize_text()/deanonymize_personal_information()`; `ANONYMIZE` из app_config; `src/constants.py::DUMMY_PERSONAL_INFO_*` | Полное переиспользование |
| Читаемый вид для промпта | `src/utils/json_to_readable.py`: `transform_resume_data`, `transform_vacancy_data` | Полное переиспользование |
| Генерация письма | `GPTAnswerer`: конструктор, `set_resume()`, `set_job()`, `write_cover_letter()`; `prompts.coverletter_template` | Полное переиспользование |
| Парсинг вакансии | `PlaywrightJobManager.get_vacancy_full_info(vacancy_url)` (:970) → dict под модель `Job` | Для `/letter <url>` |
| Отправка в Telegram | `TelegramReportSender`: PTB `Bot`, chunking 4096, прокси-логика в `__init__` | Вынести фабрику `build_ptb_request(secrets)` |
| Сессия браузера | `BROWSER_STORAGE_STATE = data_folder/browser_session/hh_state.json` | Один менеджер на процесс бота |
| Кэш/лимиты запусков | `LAST_RUN_FILE`, `JobApplier.check_the_last_search_time()`, `max_applies_num` | Без изменений |

### Как сейчас используются чат и топики (исследование, v3)

Бот работает в **одном форум-чате** `tg_chat_id` (например `@xx_feedback`); все сообщения направляются в топики через `message_thread_id`:

| Ключ секрета | Значение (пример) | Кто использует сегодня |
|---|---|---|
| `tg_chat_id` | `@xx_feedback` | Все отправители: `TelegramReportSender`, `AsyncTelegramSink`, captcha-логика |
| `tg_report_topic_id` | 344 | `TelegramReportSender.send_telegram_report()` — финальный отчёт после рассылки (`_send_chunked_messages`, plain text, chunking 4096) |
| `tg_jobs_topic_id` | 345 | `TelegramReportSender.send_job_description()` — карточка вакансии + письмо после успешного отклика (`_format_job_message`, HTML parse mode, `html.escape`), опционален: если не задан, отправка молча пропускается |
| `tg_err_topic_id` | 5 | `AsyncTelegramSink` (`src/telegram/telegram_error_handler.py:86`) — лог ошибок из loguru |
| `tg_captcha_topic_id` | 17 | `process_captcha` / `receive_messages` (Telethon) — пересылка капчи и чтение ответа |

Паттерны, которые наследуем: отправка всегда `(chat_id, message_thread_id)`; необязательный топик → тихий пропуск (`if not self.jobs_topic_id: return`); ошибки Telegram логируются, но не роняют поток; длинные сообщения режутся на части по 4096.

**Распределение топиков для новой фичи (зафиксировано):**

| Топик | Что туда идёт от новой фичи |
|---|---|
| **`tg_control_topic_id` (НОВЫЙ, опциональный)** | Всё интерактивное: ответы на команды (`/start`, `/menu`, `/status`, `/search`), inline-меню с кнопками, ProgressMessenger (live-статус поиска), финальные краткие сводки запусков (manual/scheduled), диалог установки расписания (ConversationHandler), результат `/letter` |
| `tg_jobs_topic_id` (345) | Без изменений: карточки вакансий + письма после успешных откликов из основного пайплайна (`send_job_description`). Результат `/letter` сюда **не дублируется** (решение v4, см. Decision I.4) |
| `tg_report_topic_id` (344) | Без изменений: детальный отчёт `send_telegram_report()` после каждого прогона пайплайна (триггерится самим `JobApplier.send_report()`, включая ручные запуски) |
| `tg_err_topic_id` (5) | Без изменений для AsyncTelegramSink; сюда же — критические ошибки шедулера/бота (краш job'а APScheduler, невозможность создать браузер) коротким сообщением |
| `tg_captcha_topic_id` (17) | Без изменений |

**Рекомендация по управляющему топику:** завести **отдельный** `tg_control_topic_id`. Обоснование: (1) inline-клавиатуры и диалоги ConversationHandler теряются среди потока вакансий/отчётов — кнопки «уезжают» вверх по ленте и нажимаются случайно или устаревают; (2) ProgressMessenger редактирует одно сообщение много раз — в шумном топике это создаёт визуальный мусор и гонки за внимание; (3) отдельный топик даёт чистую историю всех командных действий (кто когда запускал поиск и что менял в расписании) — фактически бесплатный аудит-лог. Fallback при отсутствии ключа: управляющие сообщения идут в `tg_report_topic_id` (не в jobs — там потоковый контент), backward compatibility полная.

### Архитектурные решения (новые в v2, дополнено в v3)

**A. Один процесс = бот + шедулер.** Новый корневой модуль `app_main.py` собирает: `PlaywrightJobManager` (лениво), `SearchScheduler`, `HhApplierBot`, запускает их в одном event loop. Шедулер и хендлеры бота ставят задачи в единую **`TaskQueue`** (см. решение E), worker которой последовательно исполняет `run_search_pipeline()` / генерацию письма.

**B. Механизм шедулирования — APScheduler 3.x (`AsyncIOScheduler`).**
Обоснование выбора:
- даёт `CronTrigger` (полноценные cron-выражения: «в 9:00 ежедневно», «по будням» и т.д.) без самописного парсера;
- нативная обработка пропусков: `misfire_grace_time` (сколько секунд после планового времени запуск ещё считается актуальным) + `coalesce=True` (один догоняющий запуск вместо серии);
- `AsyncIOScheduler` живёт в том же event loop, что и PTB-бот, — не нужен отдельный поток/процесс;
- зрелая, стабильная библиотека ~0 дополнительных транзитивных зависимостей.
Альтернатива (самописный цикл «раз в минуту сверяем cron-строку» + `croniter`) отвергнута: придётся самому решать misfire/drift/DST, а выигрыш — одна зависимость меньше.

**C. Персистентность расписания** — файл **`data_folder/schedule/schedule.yaml`** (не секреты: это пользовательский конфиг, а `data_folder` уже смонтирован как volume и переживает пересоздание контейнера):
```yaml
enabled: true
cron: "0 9 * * *"        # cron-выражение
timezone: "Europe/Kaliningrad"
misfire_grace_time_sec: 3600   # догонять пропущенный запуск в течение часа (решение v4)
```
При старте приложения файл читается; если нет/битый — дефолт `"0 9 * * *"`, enabled=true. Изменения из бота атомарно перезаписывают файл (`save_yaml_file` из `src/utils/utils.py`). APScheduler job при изменении расписания пересоздаётся (`scheduler.reschedule_job` / remove+add).

**D. Часовой пояс.** Хранится в `schedule.yaml`, передаётся в `CronTrigger(timezone=...)`. Дефолт **`Europe/Kaliningrad`** (решение v4). Валидация через `zoneinfo.ZoneInfo`.

**E. Очередь задач вместо lock'а (переработано в v4).** Единый объект `TaskQueue` (см. Phase 2): последовательная `asyncio.Queue` + одна worker-корутина — задачи выполняются строго по одной (браузер один, конкурентность недопустима). Каждая задача помечена источником:

- `"scheduled"` — триггер APScheduler;
- `"manual_search"` — `/search` или пункт меню «Искать сейчас»;
- `"letter"` — `/letter` (генерация письма).

Семантика при занятости:
- **никаких отказов и пропусков**: любая новая задача ставится в хвост очереди; пользователь получает ответ «⏳ В очереди: позиция N»;
- worker берёт следующую задачу сразу после завершения предыдущей;
- **дедупликация scheduled**: если в очереди уже есть задача с source=`"scheduled"` — повторный триггер шедулера новую не добавляет (warning в лог); manual-задачи не дедуплицируются (пользователь явно попросил);
- ограничение размера очереди (`maxsize`, напр. 10) — при переполнении ручные задачи отклоняются понятным сообщением «Очередь переполнена, попробуйте позже» (защита от спама), scheduled — молча пропускается с warning.

**F. Пропущенные запуски — catch-up подтверждён (v4).** `misfire_grace_time_sec` из конфига (дефолт 3600): если процесс был недоступен в плановое время (перезапуск контейнера, проблемы на сервере, недоступность LLM) и проснулся в пределах grace — APScheduler выполнит один догоняющий запуск (`coalesce=True`). Позже grace → запуск пропущен до следующего cron. Независимая страховка от задвоения:
1. дедупликация scheduled-задач в очереди (см. E);
2. условие постановки misfire-догона: при старте/пробуждении догоняющий запуск ставится в очередь, **только если** очередь пуста, ничего не выполняется (`not task_queue.is_busy`) и последний успешный поиск был раньше планового времени пропущенного прогона (`last_run_info()["last_run"] < missed_fire_time` — чтение из `LAST_RUN_FILE`). Это исключает ситуацию «контейнер поднялся, поиск уже идёт/уже сделан — а misfire ставит ещё один».

**G. Меню бота** (`/menu`, inline-клавиатура):
```
🔍 Искать вакансии сейчас      → callback search:now
⏰ Текущее расписание          → callback schedule:view
✏️ Изменить расписание         → callback schedule:set (ожидание ввода текста)
🟢/🔴 Автозапуск вкл/выкл      → callback schedule:toggle
📊 Статус                      → callback status:view
```
`schedule:set` — FSM-lite на python-telegram-bot (`ConversationHandler`): бот ждёт сообщение вида `30 8 * * * Europe/Kaliningrad` или упрощённое `09:30`, валидирует (cron-парсинг через `CronTrigger.from_crontab` + `ZoneInfo`), показывает превью следующих 3 запусков (`trigger.get_next_fire_time`), кнопки «Сохранить/Отмена».

**H. Маршрутизация сообщений по топикам (v3).** Чат один — `tg_chat_id` (`@xx_feedback`, форум); вся маршрутизация через `message_thread_id`. Новый класс-хелпер `TopicRouter` (см. Phase 3) инкапсулирует выбор топика:

| Метод | Топик |
|---|---|
| `control()` → `tg_control_topic_id`, fallback `tg_report_topic_id`, fallback None (без thread) | команды, меню, статусы, ProgressMessenger, сводные итоги запусков, `/letter` |
| `jobs()` → `tg_jobs_topic_id` или None | карточки вакансий из пайплайна (`send_job_description`, без изменений) |
| `errors()` → `tg_err_topic_id` | критические ошибки шедулера/бота |
| `report()` → `tg_report_topic_id` | детальный отчёт после прогона (существующий механизм, без изменений) |

Все новые ключи опциональны; при отсутствии топика — тихий пропуск или fallback по таблице выше (паттерн уже используется в `send_job_description`).

**I. Решения, зафиксированные пользователем в v4 (все открытые вопросы закрыты):**
1. **Дефолт TZ — `Europe/Kaliningrad`** (константа `DEFAULT_SCHEDULE_TZ`, дефолт в `schedule.yaml`, примеры в README).
2. **Misfire — catch-up**: пропущенный запуск догоняется (grace-окно, coalesce). Причины пропуска ожидаются только технические (недоступность LLM, проблемы сервера).
3. **Scheduled при занятости — ОЧЕРЕДЬ**, не пропуск: задача становится в хвост, дедупликация по source=`"scheduled"`.
4. **`/letter` во время поиска — ОЧЕРЕДЬ**, не отказ; результат письма отправляется **только** в control-топик, без дубля в jobs-топик.
5. **Multi-user НЕ нужен**: один пользователь, авторизация по `tg_chat_id` + опциональному allowlist (`tg_allowed_user_ids`); одно расписание на процесс, профили не требуются.
6. **Вакансия по ссылке — Playwright** (`get_vacancy_full_info`), HH API отложен до отдельной инициативы.

### Структура данных резюме и анонимизация (из v1, без изменений)

Резюме не хранится статично — скрейпится Playwright'ом; кэш `data_folder/output/resume.yaml` только для fallback/отладки. Dict: `personal_information` (имя/пол/контакты/зарплата), `experience`, `skills`, `educations`, `about_me`, `previous_job_details`. В промпт LLM идёт `transform_resume_data(dict)` + отдельно `sex`/`first_name`. Анонимизация (`ANONYMIZE` + `DUMMY_PERSONAL_INFO_*`) до LLM и деанонимизация ответа обязательны.

---

## Implementation Phases

### Phase 1: Рефакторинг — переиспользуемый пайплайн поиска (из v1, валидна)

**Objective:** Извлечь `create_and_run_bot()` из `main.py` в сервис с progress-callback, параметрами `force` и внешним `manager`.

**Files to Modify/Create:**
- `src/job_manager/pipeline_runner.py` (новый):
  ```python
  async def run_search_pipeline(
      secrets: dict,
      parameters: dict,
      llm_api_key: str,
      llm_proxy: list[str],
      progress_cb=None,            # async (stage: str, detail: str) -> None
      force: bool = False,          # игнорировать суточный лимит check_the_last_search_time
      manager=None,                 # внешний PlaywrightJobManager (для long-running процесса)
  ) -> dict:  # {"success_applies": int, "stopped_reason": str}
  ```
  Если `manager` передан — не создавать и **не закрывать** свой (владелец — приложение); иначе создать/закрыть как раньше. На этапах login/resume/search/page-N дёргать `progress_cb` (все ошибки колбэка глотаются с logger.error).
- `main.py`: заменить тело `create_and_run_bot()` на вызов `run_search_pipeline(...)` (поведение прежнее).

**Tests to Write:** `tests/test_pipeline_runner.py`
- `test_run_search_pipeline_orchestrates_components` (порядок set_parameters→set_resume→set_gpt_answerer→start_apply; close() при исключении)
- `test_external_manager_not_closed`
- `test_force_bypasses_last_search_check`
- `test_progress_cb_called_on_stages`, `test_progress_cb_error_is_swallowed`

**Acceptance Criteria:**
- [ ] Старые `tests/test_main.py` зелёные без правок; чистый refactor

---

### Phase 2: TaskQueue + SearchRunner — последовательная очередь задач для шедулера, ручного запуска и писем

**Objective:** Единая точка постановки и исполнения браузероёмких задач: последовательный worker, статусы (в очереди / выполняется / свободен), дедупликация scheduled-задач, различие источников, интеграция с прогрессом в Telegram.

**Files to Modify/Create:**
- `src/application/task_queue.py` (новый):
  ```python
  @dataclass
  class Task:
      source: str                  # "scheduled" | "manual_search" | "letter"
      coro_factory: Callable[[], Coroutine]   # задача, создаётся в момент старта
      progress_cb: Callable | None = None
      on_done: Callable[[dict | Exception], Coroutine | None] | None = None
  
  class TaskQueue:
      def __init__(self, maxsize: int = 10): ...
      async def start(self) -> None:
          """Запускает worker-корутину: while True: task = await queue.get(); ..."""
      async def stop(self) -> None: ...
      def enqueue(self, task: Task) -> int | None:
          """
          Ставит задачу в очередь. Возвращает позицию (1 = выполняется сейчас).
          None — если очередь переполнена или это дубликат scheduled.
          """
      def dedup_key(self, task) -> bool:
          """True только для source="scheduled" при наличии такой же в очереди."""
      @property
      def is_busy(self) -> bool: ...
      def current(self) -> Task | None: ...
      def position_of(self, task_id) -> int | None: ...
      def size(self) -> int: ...
  ```
  Worker исполняет задачи строго последовательно; исключение задачи ловится, передаётся в `on_done`, worker продолжает работу. Дедупликация — по списку ожидающих задач (source == "scheduled").
- `src/application/search_runner.py` (новый) — исполнители задач (без своей конкурентности):
  ```python
  class SearchRunner:
      def __init__(self, secrets, parameters, llm_api_key, llm_proxy, manager_factory):
          ...
      async def run_search(self, *, force=False, progress_cb=None) -> dict:
          """Вызывается ТОЛЬКО из worker'а TaskQueue."""
      async def generate_letter(self, text: str, resume_cache) -> str:
          """Аналогично — только из worker'а (см. Phase 6)."""
      def last_run_info(self) -> dict: ...   # чтение LAST_RUN_FILE (last_run, success_applies_num)
  ```
  Внутри: `manager = await manager_factory()` → `run_search_pipeline(..., manager=manager)`.
- `src/constants.py`: константа пути `SCHEDULE_FILE` (сама схема — решение C).

**Tests to Write:** `tests/test_task_queue.py`, `tests/test_search_runner.py`
- `test_tasks_execute_sequentially_in_order`: три задачи с разными длительностями → порядок завершения = порядок постановки, пересечений нет
- `test_enqueue_returns_position` (1 при простое, N при заполненной очереди)
- `test_scheduled_deduplication`: две scheduled подряд → вторая отклонена (None), одна manual между ними не мешает
- `test_manual_not_deduplicated`
- `test_queue_overflow_rejects`
- `test_worker_survives_task_exception` (следующая задача выполняется после упавшей)
- `test_is_busy_and_current_tracking`
- `test_run_search_uses_external_manager_and_pipeline` (мок run_search_pipeline)
- `test_last_run_info_reads_cache_file` (tmp_path с last_run.yaml)

**Acceptance Criteria:**
- [ ] Задачи никогда не выполняются параллельно; порядок FIFO
- [ ] Повторная scheduled-задача в очереди невозможна; переполнение обрабатывается понятно

---

### Phase 3: Скелет long-running приложения: шедулер (APScheduler) + каркас бота

**Objective:** Процесс, который живёт постоянно: `AsyncIOScheduler` читает расписание из `schedule.yaml` и ставит scheduled-задачу в `TaskQueue`; бот отвечает на `/start`, `/status`, `/menu` (меню пока с рабочими «Искать сейчас»/«Статус», остальные пункты-заглушки).

**Files to Modify/Create:**
- `requirements.txt`: добавить `APScheduler>=3.10,<4`
- `src/constants.py`: `SCHEDULE_FILE = "data_folder/schedule/schedule.yaml"`, `DEFAULT_SCHEDULE_CRON = "0 9 * * *"`, `DEFAULT_SCHEDULE_TZ = "Europe/Kaliningrad"` (решение v4)
- `src/application/schedule_store.py` (новый):
  - `load_schedule(path=SCHEDULE_FILE) -> ScheduleConfig` — pydantic-модель `{enabled: bool, cron: str, timezone: str, misfire_grace_time_sec: int}` с валидацией (cron через `CronTrigger.from_crontab`, tz через `zoneinfo.ZoneInfo`); битый/отсутствующий файл → дефолт + warning
  - `save_schedule(cfg, path=...)` — атомарная запись (tmp + replace)
- `src/application/search_scheduler.py` (новый) — класс `SearchScheduler`:
  - `__init__(task_queue: TaskQueue, store: ScheduleStore)`
  - `start()`/`shutdown()`: `AsyncIOScheduler(timezone=cfg.timezone)`; добавление job `self._fire` c `CronTrigger.from_crontab(cfg.cron, timezone=cfg.timezone)`, `misfire_grace_time=cfg.misfire_grace_time_sec`, `coalesce=True`, `max_instances=1`, id="daily_search"
  - `apply_schedule(cfg)`: пересоздание job при изменении расписания из бота
  - `_fire()`: `task_queue.enqueue(Task(source="scheduled", coro_factory=self._make_search_coro))`; дедупликация и переполнение обрабатываются самой очередью (None → logger.warning «scheduled уже в очереди»); все исключения ловятся и логируются + короткое сообщение через `TopicRouter.errors()` (job не должен валить процесс)
  - **misfire-догон** (решение F): на `start()` вычислить пропущенный fire time (`trigger.get_next_fire_time(now - grace, ...)`, либо сравнение `last_fire_time < now < last_fire_time + grace`); если догон актуален И очередь пуста И ничего не выполняется И `last_run_info()["last_run"] < missed_fire_time` — поставить одну scheduled-задачу (защита от задвоения: дедупликация очереди + проверка last_run)
  - `next_fire_times(n=3) -> list[datetime]` — для превью в меню
- `src/telegram/telegram_bot.py` (новый) — класс `HhApplierBot` (каркас из v1 + long-running):
  - фабрика `build_ptb_request(secrets)` (прокси `tg_proxy` → `llm_proxy[0]`) — вынести из `TelegramReportSender.__init__`, отрефакторить `telegram_manager.py` на неё
  - **`TopicRouter`** (v3, см. решение H): методы `control()/jobs()/errors()/report()` возвращают `(chat_id, message_thread_id)` по таблице решения H; fallback-цепочка для control: `tg_control_topic_id` → `tg_report_topic_id` → None
  - `_authorized(update)`: только `tg_chat_id` или `tg_allowed_user_ids`
  - `/start` (приветствие + меню), `/status` (свободен/занят + источник текущей задачи + длина очереди + последний прогон из `runner.last_run_info()` + следующее время по расписанию), `/menu` (inline-клавиатура) — все ответы через `router.control()`
  - CallbackQueryHandler: `search:now`, `schedule:view/set/toggle`, `status:view` (search:now реализуется в Phase 4, schedule:* — в Phase 5)
- `src/views/config.py`: в `Secrets` добавить опциональные поля (+ field_validator «строка "123,456" → [123,456]» по образцу `validate_tg_api_id`):
  - `tg_control_topic_id: Optional[int] = None` — управляющий топик; при None → fallback на `tg_report_topic_id` в `TopicRouter`
  - `tg_allowed_user_ids: Optional[List[int]] = []`
- `app_main.py` (корень, новый) — точка входа long-running: валидация секретов/конфига → ленивая фабрика `PlaywrightJobManager` → `SearchRunner` → `TaskQueue` (`await task_queue.start()`) → `ScheduleStore` → `SearchScheduler.start()` (включая misfire-догон) → `HhApplierBot(...)` + graceful shutdown (SIGTERM/SIGINT: `scheduler.shutdown()`, `await task_queue.stop()` — дождаться/корректно снять текущую задачу, `await manager.close()`)

**Tests to Write:**
- `tests/test_schedule_store.py`:
  - `test_load_defaults_when_file_missing`
  - `test_load_valid_file_roundtrip`
  - `test_invalid_cron_falls_back_to_default`
  - `test_invalid_timezone_falls_back_to_default`
  - `test_save_atomic_write`
- `tests/test_search_scheduler.py`:
  - `test_start_registers_job_with_cron_trigger` (инспект `scheduler.get_job("daily_search").trigger`)
  - `test_fire_enqueues_scheduled_task` (мок task_queue; задача с source="scheduled" в очереди)
  - `test_fire_duplicate_scheduled_not_enqueued` (queue.enqueue вернул None → warning, без исключения)
  - `test_fire_swallows_exception`
  - `test_apply_schedule_reschedules_job`
  - `test_misfire_settings_passed` (grace_time, coalesce)
  - `test_misfire_catchup_on_start_when_queue_empty_and_last_run_older` → одна scheduled-задача поставлена
  - `test_misfire_skipped_when_task_already_running_or_queued`
  - `test_misfire_skipped_when_search_already_done_after_missed_time` (`last_run >= missed_fire_time`)
  - `test_default_tz_is_kaliningrad`
- `tests/test_telegram_bot.py`:
  - `test_unauthorized_update_ignored`
  - `test_status_reports_idle_busy_and_queue_length` (занятость и позиция из task_queue)
  - `test_menu_shows_inline_keyboard`
- `tests/test_topic_router.py` (v3):
  - `test_control_uses_control_topic_when_set` → `(tg_chat_id, tg_control_topic_id)`
  - `test_control_falls_back_to_report_topic` (`tg_control_topic_id` отсутствует)
  - `test_control_falls_back_to_no_thread` (нет обоих) → `message_thread_id=None`
  - `test_jobs_errors_report_routing`

**Acceptance Criteria:**
- [ ] `python app_main.py` поднимает бота и шедулер; при наступлении cron-времени поиск стартует (проверено тестами на моках)
- [ ] Расписание переживает перезапуск процесса (файл)
- [ ] Все ответы бота идут в правильный топик (`message_thread_id`), fallback-цепочка control-топика работает
- [ ] Прокси-логика не дублируется; `tests/test_telegram_manager.py` зелёные

---

### Phase 4: Ручной запуск /search и пункт меню «Искать сейчас» (v4: постановка в очередь)

**Objective:** `/search [force]` и callback `search:now` ставят manual_search-задачу в `TaskQueue`; пользователь видит позицию в очереди, затем live-статус и финальную сводку.

**Files to Modify/Create:**
- `src/telegram/telegram_bot.py` — расширение:
  - Хендлер `/search`: `force`-аргумент или кнопка подтверждения (callback `search:confirm:force` / `search:cancel`) при суточном лимите; затем `position = task_queue.enqueue(Task(source="manual_search", coro_factory=..., progress_cb=messenger))`
  - Ответ пользователю по позиции: `1` → «🚀 Запускаю поиск…»; `N > 1` → «⏳ Задача в очереди (позиция N), начнётся после завершения текущей»; `None` (переполнение) → «Очередь переполнена, попробуйте позже»
  - `_on_manual_done(result)`: сводка (кол-во откликов, причина остановки) через `router.control()`; исключение → короткое сообщение, полный tb в лог
  - ProgressMessenger активируется только при старте задачи (в `coro_factory`, не при enqueue), чтобы статус не «висел» пока задача ждёт очереди
- `src/telegram/progress_messenger.py` (новый) — `ProgressMessenger`: создаёт статус-сообщение и редактирует его **в control-топике** (`router.control()` → `message_thread_id`), троттлинг ≥10 сек между `edit_text`, ошибки глотаются; финальная краткая сводка («✅ Поиск завершён: N откликов, причина …») — тоже в control-топик (детальный отчёт по-прежнему уходит в `tg_report_topic_id` через существующий `JobApplier.send_report()` — дублей нет, у сообщений разная детализация)
- Rate limits hh.ru: `force`-подтверждение + существующие `check_the_last_search_time()`, `max_applies_num`, паузы `MINIMUM_WAIT_TIME_SEC`

**Tests to Write:** `tests/test_telegram_bot_search.py`
- `test_search_enqueues_manual_task` (мок task_queue; source="manual_search")
- `test_search_reports_queue_position` (позиция N > 1 → сообщение «в очереди»)
- `test_search_rejects_on_queue_overflow`
- `test_search_requires_confirmation_without_force`
- `test_search_reports_result_via_on_done` (мок SearchRunner.run_search)
- `test_search_failure_sends_error_message`
- `test_progress_starts_only_when_task_begins`
- `test_progress_messenger_throttles_edits`

**Acceptance Criteria:**
- [ ] `/search` никогда не «теряется»: при занятости задача встаёт в очередь, пользователь знает позицию
- [ ] Ошибка задачи не валит worker и процесс

---

### Phase 5: Меню управления расписанием

**Objective:** Просмотр текущего расписания, изменение (cron или «ЧЧ:ММ»), вкл/выкл автозапуска — с персистентностью и мгновенным применением.

**Files to Modify/Create:**
- `src/application/schedule_store.py` — дополнить `validate_user_input(text) -> tuple[bool, ScheduleConfig|str]`: принимает `"<cron>"`, `"<cron> <TZ>"` или `HH:MM` (= `M H * * *`); возвращает конфиг либо человекочитаемую ошибку
- `src/telegram/telegram_bot.py` — хендлеры:
  - `schedule:view`: показать `enabled`, cron, tz, следующие 3 запуска (`scheduler.next_fire_times(3)`), кнопку «Изменить»/«Вкл/Выкл»
  - `schedule:set` — `ConversationHandler`: состояние ожидания ввода → `validate_user_input` → превью («Применять каждые …, следующие запуски: …») с кнопками «💾 Сохранить» (`schedule:apply:<hash>`)/«❌ Отмена»; при сохранении — `store.save_schedule(cfg)` + `scheduler.apply_schedule(cfg)` + подтверждение
  - `schedule:toggle`: invert `enabled`; если off — удалить job из шедулера (расписание сохраняется в файле); if on — восстановить
- Edge cases: шедулер выключен (`enabled=false`) — `/status` показывает «автозапуск отключён»; попытка `toggle` во время выполняющегося поиска/очереди — разрешена (влияет только на будущие триггеры; уже стоящие в очереди scheduled-задачи не отзываются — допустимо, т.к. дедупликация не даст добавить новые)

**Tests to Write:** `tests/test_schedule_menu.py`
- `test_validate_accepts_cron_with_tz`, `test_validate_accepts_hhmm`, `test_validate_rejects_garbage_with_message`
- `test_set_conversation_saves_and_applies` (моки store+scheduler; файл записан, job пересоздан)
- `test_toggle_off_removes_job_keeps_file`
- `test_toggle_on_restores_job`
- `test_view_shows_next_fire_times`

**Acceptance Criteria:**
- [ ] Новое расписание применяется без перезапуска контейнера и переживает его
- [ ] Некорректный ввод не ломает состояние, пользователь получает понятную ошибку

---

### Phase 6: Извлечение вакансии + /letter (v4: через очередь, без дубля в jobs)

**Objective:** `/letter <ссылка|текст>` → letter-задача в `TaskQueue` → письмо в control-топик через существующий механизм резюме и анонимизации.

**Files to Modify/Create:**
- `src/job_manager/vacancy_extractor.py` (новый): regex `hh\.ru/vacancy/(\d+)` → `manager.get_vacancy_full_info(url)` → `Job(**job).model_dump()`; без ссылки — `Job(job_title="Вакансия из чата", description=text)` (обрезка ~15000 символов). **Playwright-скрейп — принятое решение (v4); HH API отложен.**
- `src/llm/cover_letter_service.py` (новый) — `CoverLetterService`: TTL-кэш резюме (6 ч) поверх `ResumeScraper.get_id_of_selected_resume()+get_resume_info()`; fallback на `data_folder/output/resume.yaml` при недоступном браузере; `generate(text) -> str` = extract → `GPTAnswerer.set_resume/set_job` → `write_cover_letter()` → `deanonymize_personal_information`
- `src/telegram/telegram_bot.py` — хендлер `/letter`: пустой аргумент → подсказка; иначе `task_queue.enqueue(Task(source="letter", coro_factory=...))` с ответом по позиции («⏳ В очереди: N» / «🚀 Генерирую…» при позиции 1); результат — **только** в control-топик (без дубля в jobs-топик, решение v4); ошибка → сообщение
- Конкурентность: `/letter` выполняется тем же worker'ом очереди, что и поиск, — гонка за единственный `PlaywrightJobManager` исключена архитектурно (никаких семафоров/отказов не нужно)

**Tests to Write:** `tests/test_vacancy_extractor.py`, `tests/test_cover_letter_service.py`, `tests/test_telegram_bot_letter.py`
- extractor: URL-варианты (https/без схемы/utm), не-URL текст, мок скрейпа, обрезка длинного текста
- service: TTL-кэш (один скрейп на два вызова), refresh после TTL, деанонимизация выхода, job из URL и текста, ошибка LLM → читаемое исключение
- bot: пустые аргументы, постановка letter-задачи в очередь + позиция, результат только в control-топик, успешная отправка письма
- queue-integration: letter-задача, поставленная во время поиска, выполняется строго после него

**Acceptance Criteria:**
- [ ] Резюме — существующий механизм (скрейп + TTL), персональные данные анонимизируются до LLM
- [ ] `/letter` во время поиска ставится в очередь и исполняется после; результат только в control-топик

---

### Phase 7: Docker/эксплуатация и документация

**Objective:** Контейнер становится long-running, CMD меняется на нового entrypoint.

**Files to Modify/Create:**
- `Dockerfile`: `COPY app_main.py ./` ; `CMD ["python", "app_main.py"]` (старый `main.py` остаётся в образе для разового локального прогона)
- `docker-compose.yml`: `restart: "no"` → `restart: unless-stopped`; volume `./data_folder:/app/data_folder` уже покрывает персистентность `schedule.yaml`, `last_run.yaml`, browser-сессии — дополнительно ничего не нужно
- `data_folder/secrets/secrets.yaml` + `data_folder_example/secrets/secrets.yaml`: задокументировать новые опциональные ключи (v3):
  ```yaml
  # --- Telegram (существующий форум-чат @xx_feedback, менять chat_id не нужно) ---
  tg_control_topic_id: 346   # НОВЫЙ, опциональный: топик для команд/меню/статусов/прогресса.
                             # Если не задан — управляющие сообщения идут в tg_report_topic_id
  tg_allowed_user_ids: []    # НОВЫЙ, опциональный: allowlist user_id; пусто = только tg_chat_id
  ```
  Backward compatibility: все новые ключи опциональны с дефолтами (`tg_control_topic_id=None` → fallback на `tg_report_topic_id`; `tg_allowed_user_ids=[]` → доступ только по `tg_chat_id`); существующие ключи (`tg_chat_id`, `tg_err/captcha/report/jobs_topic_id`) не меняются и остаются обязательными/опциональными как раньше. Валидация `Secrets` принимает старые конфиги без правок.
- `README.md`: раздел «Long-running режим»: архитектура (бот + внутренний шедулер + последовательная очередь задач), команды (`/start /menu /status /search /letter`), формат расписания (`cron TZ`, `HH:MM`), misfire-поведение (catch-up в течение grace-окна) и часовой пояс (дефолт `Europe/Kaliningrad`), семантика очереди (позиция в очереди, дедупликация scheduled), суточный лимит hh.ru и `force`; **таблица распределения топиков** из решения H и инструкция «создайте в @xx_feedback новый топик для управления ботом и впишите его ID в `tg_control_topic_id`» (ID узнать через существующий `/id`-скрипт `get_telegram_chat_and_topic_id.py`); note: внешний cron больше не нужен
- Graceful shutdown: проверить, что SIGTERM (docker stop) завершает поиск корректно или помечает его прерванным (кэш `last_apply` уже позволяет продолжить счётчики при следующем старте — `_check_the_previous_apply_number`)

**Tests to Write:**
- `tests/test_views_config.py`: `Secrets` с `tg_allowed_user_ids: "123,456"` → `[123, 456]`; `tg_control_topic_id` опционален (None по умолчанию); обратная совместимость старых secrets без новых ключей
- `tests/test_app_main.py`: сборка приложения с мок-компонентами (bot+scheduler создаются, shutdown чистит ресурсы)

**Acceptance Criteria:**
- [ ] `docker compose up -d` поднимает постоянный контейнер; `schedule.yaml` в volume переживает recreate
- [ ] `docker stop` не оставляет битых состояний (очередь/кэш)
- [ ] `pre-commit run --all-files` чистый

---

## Testing Strategy (сводно)

- **pytest 8.3 + pytest-asyncio 0.26 + pytest-mock**, маркер `@pytest.mark.asyncio`; coverage по `src` включён в `pytest.ini`
- **Шедулер тестируется детерминированно**: не ждём реального времени — проверяем конфигурацию job'ов (`get_job().trigger`, `misfire_grace_time`, `coalesce`) и вызываем `_fire()` напрямую с моком `TaskQueue`; для проверки cron-парсинга — `CronTrigger.from_crontab(...)` + `get_next_fire_time` на фиксированном `now` (API APScheduler принимает `now`); misfire-catchup проверяется подстановкой синтетических `last_run`/`missed_fire_time`
- **Очередь тестируется на реальных asyncio-примитивах**: `asyncio.Queue` + worker запускаются в тесте, задачи — короткие корутины с `asyncio.sleep(0)`/событиями; проверяем FIFO-порядок, дедупликацию scheduled, позиции, переполнение, выживание worker'а после исключения (см. `tests/test_task_queue.py`)
- **Моки внешних границ:** `PlaywrightJobManager`, LLM-цепочки, PTB `Bot`, файловая система через `tmp_path` — реальные hh.ru/Telegram/LLM в тестах запрещены
- **conftest:** существующие `mock_telegram_sink*` фикстуры защищают от случайных отправок
- После каждой фазы — полный `pytest`

## Risks & Mitigations

| Риск | Митигация |
|---|---|
| APScheduler job падает и молча умирает | try/except вокруг `_fire` + logger.error + сообщение в err-топик; `max_instances=1` |
| Поиск длится дольше интервала до следующего cron-запуска | scheduled-задача становится в очередь (решение v4); дедупликация не даст накопиться дублям |
| Неконтролируемый рост очереди (спам командами) | `maxsize=10`; ручные задачи при переполнении отклоняются сообщением, scheduled — пропускаются с warning |
| Misfire-догон задваивает поиск | Тройная защита: coalesce + дедупликация scheduled в очереди + условие `last_run < missed_fire_time` при постановке |
| Задача «letter» застряла позади длинного поиска | Пользователь видит позицию в очереди; поиск ограничен `max_applies_num`/`applies_num<400` |
| Контейнер перезапущен во время поиска | Кэш `last_run.yaml`/`last_apply` восстанавливает счётчики (`_check_the_previous_apply_number`); misfire догоняет пропущенное расписание |
| Неверный TZ пользователя → поиск не в то время | Валидация `ZoneInfo` при вводе + превью следующих 3 запусков перед сохранением |
| DST-переходы | `CronTrigger(timezone=...)` обрабатывает штатно; храним IANA-имя зоны, не offset |
| Один PlaywrightJobManager: краш браузера | Ленивая инициализация + пересоздание менеджера с retry в factory |
| Rate limit Telegram при edit_text | Троттлинг ProgressMessenger ≥10 сек |
| Суточный лимит hh.ru | `force`-подтверждение + `check_the_last_search_time()` в пайплайне |
| Персональные данные в LLM | Не трогать анонимизацию; контракт фиксирует `test_generate_deanonymizes_output` |

## Open Questions

**Открытых вопросов нет** — все решения зафиксированы пользователем в v4 (см. Decision I).

Мелочи, которые решаются по ходу имплементации (не блокируют):
- точное значение `maxsize` очереди (дефолт 10) — подобрать при нагрузочном прогоне;
- тексты сообщений бота («в очереди», «переполнена») — финализировать при ручной проверке UX;
- нужен ли отдельный callback для отмены задачи из очереди (`search:cancel:<id>`) — можно добавить позже, v4 не требует.

## Success Criteria
- [ ] Все фазы завершены; `pre-commit run --all-files` проходит
- [ ] Контейнер long-running: `restart: unless-stopped`, внешний cron не нужен
- [ ] Шедулер внутри контейнера ставит задачу в очередь по cron из `schedule.yaml` (дефолт TZ `Europe/Kaliningrad`); расписание переживает рестарты; misfire догоняется без задвоения
- [ ] Единая последовательная очередь: scheduled/manual_search/letter выполняются строго по одной, позиции сообщаются пользователю, scheduled дедуплицируются
- [ ] Все сообщения бота направляются в правильные топики `@xx_feedback`; интерактив (меню/статусы/прогресс/письма) — в `tg_control_topic_id` (или fallback на report-топик); результат `/letter` не дублируется в jobs
- [ ] Меню бота: просмотр/изменение расписания, вкл/выкл автозапуска, ручной `/search` со статусом
- [ ] Авторизация: один пользователь, `tg_chat_id` + опциональный allowlist
- [ ] `/letter` работает через существующий механизм резюме + анонимизации; вакансия по ссылке — через Playwright
- [ ] Разовый сценарий `main.py` продолжает работать
