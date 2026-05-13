# Plan: Отправка описания вакансии в Telegram после каждого отклика

**Created:** 2026-05-08
**Status:** Ready for Atlas Execution

## Summary

После каждого успешного отклика на вакансию нужно асинхронно отправлять в отдельный Telegram-топик сообщение с описанием одной этой вакансии. Для этого добавляем новый секрет `tg_jobs_topic_id`, метод `send_job_description()` в `TelegramReportSender`, и вызов этого метода в `job_applier.py` сразу после успешного отклика.

## Context & Analysis

**Relevant Files:**
- `src/telegram/telegram_manager.py`: содержит `TelegramReportSender` с `self.bot`, `self.chat_id` и `_send_chunked_messages()` — переиспользуем
- `src/job_manager/job_applier.py`: метод `send_repsonse()` — точка интеграции; после `apply_job()` возвращает `result`, здесь добавляем вызов отправки
- `src/job_manager/bot_facade.py`: создаёт `JobApplier`, нужно убедиться что `TelegramReportSender` доступен через `JobApplier`
- `src/views/job.py`: `JobDescription(job_title, company_name, vacancy_id, link, skills, cover_letter, job_score)` — это данные для сообщения
- `data_folder/secrets/secrets.yaml`: добавляем `tg_jobs_topic_id`
- `data_folder_example/secrets/secrets.yaml`: добавляем `tg_jobs_topic_id` для документации

**Key Functions/Classes:**
- `TelegramReportSender._send_chunked_messages(message, header)` в `telegram_manager.py`: уже умеет отправлять в topic через `message_thread_id`, переиспользуем
- `JobApplier.send_repsonse(vacancy)` в `job_applier.py`: вызывает `apply_job()`, получает `result` — здесь добавляем async-вызов
- `JobApplier.apply_job(...)` в `job_applier.py`: создаёт `JobDescription(...)` и сохраняет в файл — здесь же строим объект для отправки
- `BotFacade` в `bot_facade.py`: оркестрирует компоненты, через него `TelegramReportSender` уже используется для финального отчёта

**Dependencies:**
- `python-telegram-bot`: уже используется (`Bot.send_message`)
- `asyncio`: уже используется в `_send_chunked_messages`

**Patterns & Conventions:**
- Telegram-отправка уже асинхронная через `asyncio.run()` снаружи и `async def` внутри
- Секреты читаются из `SECRETS_FILE` (dict) — ключи `tg_token`, `tg_chat_id`, `tg_report_topic_id`, `tg_err_topic_id`
- Ошибки Telegram логируются, но не останавливают основной поток (обёртка в try/except в `_send_chunked_messages`)
- `JobApplier` принимает компоненты в конструктор — `telegram_sender` передаём аналогично

---

## Implementation Phases

### Phase 1: Добавить `tg_jobs_topic_id` в конфиги секретов

**Objective:** Зарегистрировать новый секрет для топика вакансий, чтобы валидация не падала.

**Files to Modify:**
- `data_folder/secrets/secrets.yaml`: добавить поле `tg_jobs_topic_id: <id>`
- `data_folder_example/secrets/secrets.yaml`: добавить `tg_jobs_topic_id: <id>` как пример

**Steps:**
1. Открыть `data_folder/secrets/secrets.yaml`, добавить строку:
   ```yaml
   tg_jobs_topic_id: 0
   ```
   (рядом с `tg_report_topic_id` и `tg_err_topic_id`)
2. Сделать то же самое в `data_folder_example/secrets/secrets.yaml`

**Acceptance Criteria:**
- [ ] Оба файла содержат поле `tg_jobs_topic_id`
- [ ] Формат YAML валиден

---

### Phase 2: Добавить метод `send_job_description()` в `TelegramReportSender`

**Objective:** Создать специализированный метод отправки одной вакансии в отдельный топик.

**Files to Modify:**
- `src/telegram/telegram_manager.py`

**Steps:**
1. В `__init__` `TelegramReportSender` добавить:
   ```python
   self.jobs_topic_id = secrets.get("tg_jobs_topic_id")
   ```
2. Добавить новый async метод `_send_to_jobs_topic(message: str)`:
   ```python
   async def _send_to_jobs_topic(self, message: str):
       try:
           await self.bot.send_message(
               chat_id=self.chat_id,
               message_thread_id=self.jobs_topic_id,
               text=message,
           )
       except TelegramError as e:
           logger.error(f"Failed to send job description to Telegram: {e}")
   ```
3. Добавить публичный метод `send_job_description(job_description)`:
   ```python
   def send_job_description(self, job_description) -> None:
       """Асинхронно отправляет описание одной вакансии в jobs-топик."""
       if not self.jobs_topic_id:
           return
       message = self._format_job_message(job_description)
       asyncio.run(self._send_to_jobs_topic(message))
   ```
4. Добавить приватный метод `_format_job_message(job_description) -> str`:
   ```python
   def _format_job_message(self, job_description) -> str:
       skills_str = ", ".join(job_description.skills) if job_description.skills else "—"
       return (
           f"📋 <b>{job_description.job_title}</b>\n"
           f"🏢 {job_description.company_name}\n"
           f"⭐ Оценка: {job_description.job_score}\n"
           f"🛠 Навыки: {skills_str}\n"
           f"🔗 {job_description.link}\n\n"
           f"<b>Сопроводительное письмо:</b>\n{job_description.cover_letter}"
       )
   ```
   И передать `parse_mode="HTML"` в `send_message`.

**Acceptance Criteria:**
- [ ] `TelegramReportSender` инициализирует `jobs_topic_id` из секретов
- [ ] Метод `send_job_description()` форматирует и отправляет сообщение
- [ ] Если `tg_jobs_topic_id` не задан (0 или None) — метод выходит без ошибок
- [ ] Ошибки Telegram логируются, но не поднимаются выше

---

### Phase 3: Передать `TelegramReportSender` в `JobApplier`

**Objective:** Дать `JobApplier` доступ к `TelegramReportSender` без нарушения существующей архитектуры.

**Files to Modify:**
- `src/job_manager/job_applier.py`: добавить параметр `telegram_sender` в конструктор
- `src/job_manager/bot_facade.py`: передать уже созданный `telegram_sender` в `JobApplier`

**Steps:**
1. В `job_applier.py` в `__init__` добавить:
   ```python
   def __init__(self, ..., telegram_sender=None):
       ...
       self.telegram_sender = telegram_sender
   ```
2. В `bot_facade.py` найти место создания `JobApplier` и добавить передачу:
   ```python
   job_applier = JobApplier(
       ...,
       telegram_sender=self.telegram_sender,  # TelegramReportSender уже есть в BotFacade
   )
   ```
   Если `TelegramReportSender` не создаётся в `BotFacade` — создать его там же, где он создаётся для финального отчёта (один экземпляр на весь запуск).

**Acceptance Criteria:**
- [ ] `JobApplier.__init__` принимает `telegram_sender=None` (опциональный)
- [ ] `BotFacade` передаёт `telegram_sender` в `JobApplier`
- [ ] Существующая логика не изменена

---

### Phase 4: Вызвать `send_job_description()` после успешного отклика

**Objective:** После каждого успешного отклика асинхронно отправить описание вакансии.

**Files to Modify:**
- `src/job_manager/job_applier.py`: метод `send_repsonse()` или `apply_job()`

**Steps:**
1. В `apply_job(...)` после создания `job_desc = JobDescription(...)` и сохранения в файл добавить отправку в Telegram.
   
   Найти место в `apply_job()`:
   ```python
   job_desc = JobDescription(
       job_title=job.get("job_title", ""),
       ...
   )
   # Сохранение в файл
   self._save_job_description(job_desc)
   ```
   
   После `_save_job_description` добавить:
   ```python
   if self.telegram_sender:
       try:
           self.telegram_sender.send_job_description(job_desc)
       except Exception as e:
           logger.error(f"Error sending job description to Telegram: {e}")
   ```

2. Убедиться что вызов происходит только когда `cover_letter_text` уже сформирован (не в `SEARCH_MODE` и не в `SKILL_STAT_MODE`).

   Проверить логику в `apply_job()`:
   - `SEARCH_MODE=True` → ранний `return ("Skip","SEARCH_MODE")` до отправки
   - `SKILL_STAT_MODE=True` → ранний `return ("Skip","SKILL_STAT_MODE")` до отправки
   - Иначе → отклик и отправка ✓

**Acceptance Criteria:**
- [ ] После каждого успешного отклика отправляется ровно одно сообщение в Telegram
- [ ] В `SEARCH_MODE` и `SKILL_STAT_MODE` отправки нет
- [ ] Ошибка отправки не прерывает основной цикл откликов
- [ ] Сообщение содержит: компанию, название вакансии, оценку, навыки, ссылку и сопроводительное письмо

---

## Open Questions

1. **Нужна ли отправка в режиме `SEARCH_MODE`?**
   - **Option A:** Не отправлять (только реальные отклики) — логично, т.к. SEARCH_MODE не откликается
   - **Option B:** Отправлять в SEARCH_MODE тоже (для предпросмотра)
   - **Recommendation:** Option A — отправлять только при реальных откликах

2. **Нужен ли `parse_mode="HTML"` или Markdown?**
   - **Option A:** HTML — более предсказуем, меньше конфликтов с символами в тексте
   - **Option B:** MarkdownV2 — красивее, но требует эскейпинга спецсимволов во всём тексте письма
   - **Recommendation:** Option A (HTML) — сопроводительное письмо содержит произвольный текст

3. **Обрезать ли длинные сопроводительные письма?**
   - **Option A:** Разбивать на части через `_send_chunked_messages` (уже есть)
   - **Option B:** Обрезать до 4096 символов с многоточием
   - **Recommendation:** Option A — переиспользовать `_send_chunked_messages` с `jobs_topic_id`

---

## Risks & Mitigation

- **Risk:** Telegram API rate limit при большом количестве откликов подряд
  - **Mitigation:** Ошибка логируется и глотается, основной цикл не останавливается; при необходимости добавить `asyncio.sleep(1)` перед отправкой
- **Risk:** `tg_jobs_topic_id` не задан в старых конфигах пользователей
  - **Mitigation:** `secrets.get("tg_jobs_topic_id")` вернёт `None`, метод `send_job_description()` проверяет `if not self.jobs_topic_id: return`
- **Risk:** Сопроводительное письмо содержит HTML-спецсимволы (`<`, `>`, `&`)
  - **Mitigation:** Экранировать текст письма через `html.escape(cover_letter)` перед вставкой в шаблон

---

## Success Criteria

- [ ] После каждого реального отклика в заданный Telegram-топик приходит сообщение с данными вакансии
- [ ] Сообщение содержит: компанию, название, оценку, навыки, ссылку, сопроводительное письмо
- [ ] Ошибки Telegram не останавливают цикл откликов
- [ ] Если `tg_jobs_topic_id` не задан — функциональность отключена без ошибок
- [ ] Существующая логика отчётов и ошибок не затронута

---

## Notes for Atlas

- `TelegramReportSender` уже инстанциирован где-то в `BotFacade` для финального отчёта — нужно убедиться что используется один экземпляр, а не создаётся новый
- Проверь точный аргументный список `JobApplier.__init__` перед добавлением параметра
- Метод `_save_job_description` может называться иначе — найди где именно `JobDescription(...)` создаётся и сохраняется в `job_descriptions.txt`; именно там добавляй вызов `send_job_description`
- `asyncio.run()` не работает внутри уже запущенного event loop; если `apply_job` уже является `async`-функцией — используй `await self._send_to_jobs_topic(message)` напрямую вместо `asyncio.run()`
