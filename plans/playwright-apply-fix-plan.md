# Plan: Фикс Playwright — кнопка «Откликнуться» и форма сопроводительного письма

**Created:** 2026-05-12
**Status:** Ready for Atlas Execution

---

## Summary

В методе `apply_to_vacancy` и вспомогательных методах `PlaywrightJobManager` выявлены две
системные проблемы: кнопка «Откликнуться» не всегда находится из-за отсутствия явного ожидания
и недостаточных fallback-селекторов, а форма сопроводительного письма теряется из-за race
condition + преждевременной отправки формы внутри `_select_resume`. Исправление покрывает оба
сценария без изменения архитектуры.

---

## Context & Analysis

### Релевантные файлы

| Файл | Роль |
|------|------|
| `src/job_manager/playwright_manager.py` | Основной файл. Методы `apply_to_vacancy`, `_select_resume`, `_handle_interfering_messages` |
| `src/utils/browser_utils.py` | `safe_click`, `safe_fill` — базовые примитивы |

### Ключевые функции

- `apply_to_vacancy` (строки ~1020–1120) — точка входа, вся логика отклика
- `_select_resume` (~1125–1185) — выбор резюме из списка; **содержит критический баг**
- `_handle_interfering_messages` (~985–1010) — закрывает куки/попапы
- `safe_click` / `safe_fill` — timeout по умолчанию 1000 мс / нет явного ожидания

---

## Диагностика: выявленные причины

### Проблема 1 — кнопка «Откликнуться» не находится

| # | Причина | Где в коде |
|---|---------|-----------|
| 1.1 | **Race condition**: после `page.goto()` идёт только `pause_async(1, 2)`, нет `wait_for_selector` | `apply_to_vacancy`, первые строки |
| 1.2 | **Перекрытие попапами**: `_handle_interfering_messages` вызывается *после* попытки клика, а не до | порядок вызовов |
| 1.3 | **Короткий timeout**: `safe_click` вызывается без `timeout=`, т.е. 1000 мс — слишком мало | оба вызова кнопки |
| 1.4 | **Устаревшие/отсутствующие fallback-селекторы**: только `vacancy-response-link-top` и `vacancy-response-link-bottom`; в новом Magritte HH бывает `vacancy-response-link-offer` и `[data-qa*="vacancy-response"]` | `apply_to_vacancy` |
| 1.5 | **«Уже откликались» не детектируется**: возврат `"Error"` вместо `"Skip"` для уже откликнутых вакансий | после `if not clicked` |

### Проблема 2 — форма сопроводительного письма не находится

| # | Причина | Где в коде |
|---|---------|-----------|
| 2.1 | **`_select_resume` преждевременно нажимает submit** (`vacancy-response-submit-popup`) — модал закрывается *до* того, как `apply_to_vacancy` успевает найти поле сопроводительного письма | `_select_resume`, последние строки |
| 2.2 | **Race condition после `_select_resume`**: нет `wait_for_selector` для формы СП; следующая проверка идёт мгновенно | `apply_to_vacancy`, после `_select_resume` |
| 2.3 | **`_handle_interfering_messages` закрывает модал**: `notification-close-button` может матчить кнопку «×» в модале с формой СП | `_handle_interfering_messages` |
| 2.4 | **Вариант 2: нет ожидания textarea**: после клика `cl_btn_xpath` код сразу проверяет `cl_input` без ожидания появления `<textarea>` | строки CL variant 2 |
| 2.5 | **Отсутствующий вариант 3**: в новом HH форма СП может встраиваться прямо на странице вакансии (не в попапе), отдельный `<textarea>` вне обоих ныне поддерживаемых вариантов | нет обработки |

---

## Стратегия трассировки (сбор реальных селекторов)

Перед правками рекомендуется записать реальный путь отклика через Playwright:

```python
# В PlaywrightJobManager или в отдельном скрипте
async def record_apply_trace(self, vacancy_url: str):
    """Режим записи: открывает вакансию и ждёт ручного прохождения."""
    await self.context.tracing.start(screenshots=True, snapshots=True)
    await self.page.goto(vacancy_url)
    await self.page.pause()          # открывает Playwright Inspector
    await self.context.tracing.stop(path="trace_apply.zip")
    # Просмотр: playwright show-trace trace_apply.zip
```

Или через codegen (без запуска приложения):
```bash
playwright codegen https://hh.ru/vacancy/<ID> --save-trace trace.zip
```

---

## Implementation Phases

### Phase 1: Диагностика — добавить трассировку и скриншоты

**Objective:** Получить точные `data-qa`-атрибуты из реального прохождения отклика в текущем Magritte UI.

**Files to Modify:**
- `src/job_manager/playwright_manager.py` — добавить метод `record_apply_trace` и опциональные скриншоты в `apply_to_vacancy`

**Steps:**
1. Добавить в `PlaywrightJobManager` метод `record_apply_trace(vacancy_url)`:
   ```python
   async def record_apply_trace(self, vacancy_url: str, output: str = "trace_apply.zip"):
       await self.ensure_logged_in()
       await self.context.tracing.start(screenshots=True, snapshots=True, sources=True)
       await self.page.goto(vacancy_url)
       logger.info("Playwright Inspector открыт. Пройдите путь отклика вручную.")
       await self.page.pause()
       await self.context.tracing.stop(path=output)
       logger.info(f"Трассировка сохранена: {output}. Просмотр: playwright show-trace {output}")
   ```
2. Добавить вызов через `--record-trace <vacancy_url>` в `main.py` или как отдельный скрипт.
3. Пользователь запускает, проходит путь отклика вручную, затем анализирует trace.
4. По результатам trace уточнить список селекторов для Phase 2–4.

**Acceptance Criteria:**
- [ ] Метод `record_apply_trace` добавлен и вызывается при флаге `--record-trace`
- [ ] После прохождения пути в trace видны все `data-qa` элементов формы отклика

---

### Phase 2: Фикс кнопки «Откликнуться»

**Objective:** Кнопка всегда находится или возвращается корректный `"Skip"` для уже-откликнутых вакансий.

**Files to Modify:**
- `src/job_manager/playwright_manager.py` — метод `apply_to_vacancy`

**Steps:**

1. **До клика кнопки** вызвать `_handle_interfering_messages()`:
   ```python
   # было:
   await self.pause_async(1, 2)
   clicked = await safe_click(...)
   
   # станет:
   await self._handle_interfering_messages()
   await self.pause_async(0.5, 1)
   ```

2. **Заменить** два отдельных `safe_click` на явное ожидание + расширенный список fallback-селекторов:
   ```python
   APPLY_SELECTORS = [
       '[data-qa="vacancy-response-link-top"]',
       '[data-qa="vacancy-response-link-bottom"]',
       '[data-qa="vacancy-response-link-offer"]',
       '[data-qa*="vacancy-response-link"]',
   ]
   
   # Ожидание появления хотя бы одного из селекторов (5 сек)
   apply_btn_appeared = False
   for sel in APPLY_SELECTORS:
       try:
           await self.page.wait_for_selector(sel, timeout=5000)
           apply_btn_appeared = True
           break
       except Exception:
           continue
   
   clicked = False
   for sel in APPLY_SELECTORS:
       if await safe_click(self.page, sel, timeout=5000, click_all=True):
           clicked = True
           break
   ```

3. **Добавить детектирование «уже откликались»**:
   ```python
   if not clicked:
       already_applied = self.page.locator(
           '[data-qa*="vacancy-response-already"], '
           'xpath=//*[contains(., "Вы уже откликались") or contains(., "Отклик отправлен")]'
       )
       if await already_applied.count() > 0:
           return "Skip", "Уже откликались на эту вакансию"
       return "Error", "Кнопка отклика не найдена"
   ```

**Acceptance Criteria:**
- [ ] Кнопка находится через расширенный список селекторов
- [ ] Уже-откликнутые вакансии возвращают `"Skip"`, не `"Error"`
- [ ] Попапы закрываются перед попыткой клика

---

### Phase 3: Фикс `_select_resume` — убрать преждевременный submit

**Objective:** `_select_resume` должен только *выбрать* резюме, не *отправлять* заявку. Отправка остаётся в `apply_to_vacancy`.

**Files to Modify:**
- `src/job_manager/playwright_manager.py` — метод `_select_resume`

**Текущий баг:**
```python
# В конце _select_resume:
await safe_click(self.page, "[data-qa^='magritte-select-option-']", element_number=best_idx)
await self.pause_async(0.5, 1)
await safe_click(self.page, "[data-qa='vacancy-response-submit-popup']", timeout=10000)  # ← BUG
```

Клик `vacancy-response-submit-popup` внутри `_select_resume` может:
- Закрыть модал с выбором резюме **до** заполнения СП (если это финальная кнопка отправки)
- ИЛИ перейти к следующему шагу модала (СП-форма) — но код не ждёт этого перехода

**Steps:**

1. Убрать `safe_click(vacancy-response-submit-popup)` из `_select_resume`.
2. Вместо этого: после выбора резюме вернуть `True`/`False` и ждать в `apply_to_vacancy`.
3. В `apply_to_vacancy` после `_select_resume`: явно ждать появления либо СП-формы, либо финальной кнопки:
   ```python
   await self._select_resume(resume_component)
   logger.info("Выбрали резюме")
   
   # Явно ждём следующего состояния модала (до 5 сек)
   NEXT_STEP_SELECTORS = [
       '[data-qa="vacancy-response-letter-informer"]',
       '[data-qa="vacancy-response-popup-form-letter-input"]',
       '[data-qa="vacancy-response-submit-popup"]',
       'xpath=//*[text()="Откликнуться"]',
   ]
   for sel in NEXT_STEP_SELECTORS:
       try:
           await self.page.wait_for_selector(sel, timeout=5000)
           break
       except Exception:
           continue
   await self.pause_async(0.5, 1)
   ```

**Acceptance Criteria:**
- [ ] `_select_resume` не нажимает submit-кнопку
- [ ] После выбора резюме код явно ждёт появления следующего состояния

---

### Phase 4: Фикс заполнения сопроводительного письма

**Objective:** Форма СП всегда находится и заполняется до нажатия submit.

**Files to Modify:**
- `src/job_manager/playwright_manager.py` — метод `apply_to_vacancy`

**Steps:**

1. **Вариант 1**: добавить явный `wait_for_selector` перед `safe_fill`:
   ```python
   magritte_cl_form = self.page.locator('[data-qa="vacancy-response-letter-informer"]')
   if await magritte_cl_form.count() > 0:
       logger.info("Найдена форма СП (вариант 1)")
       # Ждём видимости textarea внутри формы
       try:
           await self.page.wait_for_selector(
               '[data-qa="vacancy-response-letter-informer"] textarea',
               timeout=3000
           )
       except Exception:
           pass
       await safe_fill(
           self.page,
           '[data-qa="vacancy-response-letter-informer"] textarea[name="text"]',
           cover_letter,
       )
       ...
   ```

2. **Вариант 2**: после клика «Добавить» — ждать появления `<textarea>`:
   ```python
   cl_btn_xpath = "xpath=//*[text()='Добавить' or contains(text(), 'Сопроводительное')]"
   if await self.page.locator(cl_btn_xpath).count() > 0:
       if await self.page.locator(cl_btn_xpath).first.is_visible():
           await safe_click(self.page, cl_btn_xpath, supress_warnings=True)
           # ← ДОБАВИТЬ ожидание textarea:
           try:
               await self.page.wait_for_selector(
                   '[data-qa="vacancy-response-popup-form-letter-input"]',
                   timeout=3000
               )
           except Exception:
               pass
           await self.pause_async(0.5, 1)
   ```

3. **Вариант 3** (новый — inline-форма на странице, не в попапе):
   ```python
   # После проверки варианта 2
   inline_cl = self.page.locator(
       'textarea[name="text"][data-qa*="letter"], '
       '[data-qa="vacancy-response-letter-body"] textarea'
   )
   if await inline_cl.count() > 0:
       logger.info("Найдена inline-форма СП (вариант 3)")
       await inline_cl.first.fill(cover_letter)
       await self.pause_async(0.5, 1)
   ```

4. **Исправить `_handle_interfering_messages`**: добавить проверку, что `notification-close-button` не принадлежит модалу отклика:
   ```python
   # Уточнить селектор: только те close-button, что вне vacancy-response-модала
   close_btn = self.page.locator(
       '[data-qa="notification-close-button"]:not([data-qa*="vacancy-response"])'
   )
   ```

**Acceptance Criteria:**
- [ ] СП заполняется во всех трёх вариантах отображения формы
- [ ] `_handle_interfering_messages` не закрывает модал отклика

---

### Phase 5: Диагностический logging

**Objective:** При провале каждого шага сохранять скриншот + URL + логировать структуру DOM.

**Files to Modify:**
- `src/job_manager/playwright_manager.py`

**Steps:**

1. Добавить хелпер `_debug_screenshot(step_name)`:
   ```python
   async def _debug_screenshot(self, step_name: str) -> None:
       """Сохраняет скриншот текущей страницы для диагностики."""
       try:
           path = f"debug_{step_name}_{int(datetime.now().timestamp())}.png"
           await self.page.screenshot(path=path, full_page=True)
           logger.debug(f"Скриншот сохранён: {path} (URL: {self.page.url})")
       except Exception as e:
           logger.debug(f"Не удалось сохранить скриншот: {e}")
   ```

2. Вызывать `_debug_screenshot` при ключевых сбоях:
   - Кнопка отклика не найдена → `await self._debug_screenshot("apply_btn_not_found")`
   - СП-форма не найдена → `await self._debug_screenshot("cl_form_not_found")`
   - Финальная submit-кнопка не найдена → `await self._debug_screenshot("submit_not_found")`

**Acceptance Criteria:**
- [ ] Скриншоты сохраняются при каждом сбое в `apply_to_vacancy`
- [ ] В логах есть URL страницы на момент сбоя

---

## Open Questions

1. **Является ли `vacancy-response-submit-popup` финальной кнопкой отправки или кнопкой «Далее» в мастере?**
   - **Option A:** Это финальная кнопка — тогда `_select_resume` нажимает её корректно только для вакансий без поля СП.
   - **Option B:** Это кнопка перехода к следующему шагу (СП-форме) — тогда нажатие её в `_select_resume` приводит к появлению СП, но код не ждёт этого.
   - **Recommendation:** Записать trace (Phase 1) и выяснить. До выяснения — убрать клик из `_select_resume` и передать ответственность в `apply_to_vacancy`, где контекст полный.

2. **Нужно ли обрабатывать вакансии с обязательным тестом или внешней формой?**
   - Некоторые вакансии перенаправляют на сторонний сайт работодателя.
   - **Recommendation:** Детектировать редирект и возвращать `"Skip", "Внешняя форма"`.

---

## Risks & Mitigation

- **Risk:** После убирания submit-клика из `_select_resume` вакансии без СП перестанут отправляться.
  - **Mitigation:** В `apply_to_vacancy` оставить финальный блок `submit_btn = page.locator("xpath=//*[text()='Откликнуться']")` — он поймает этот случай.

- **Risk:** Новые fallback-селекторы могут матчить неправильные элементы.
  - **Mitigation:** Использовать `click_all=True` + логирование количества найденных элементов.

- **Risk:** `_handle_interfering_messages` с уточнённым селектором может пропустить реальные уведомления.
  - **Mitigation:** Уточнение только для close-button, не трогать cookie и additional-data-collector.

---

## Success Criteria

- [ ] Кнопка «Откликнуться» находится в >95% случаев на тестовой выборке вакансий
- [ ] Сопроводительное письмо заполняется корректно для всех трёх вариантов формы
- [ ] Уже-откликнутые вакансии корректно пропускаются (`"Skip"`)
- [ ] Скриншоты сохраняются при каждом сбое
- [ ] Все существующие тесты проходят

---

## Notes for Atlas

- **Начинать с Phase 1 (trace)**: пользователь подтвердил готовность пройти путь вручную. Это
  критично для уточнения реальных `data-qa` в Phase 2–4.
- Phase 3 (убирание submit из `_select_resume`) — самое рискованное изменение; реализовывать
  после просмотра trace.
- При реализации Phase 2–4 использовать `multi_replace_string_in_file` для атомарных изменений.
- Тесты для `apply_to_vacancy` находятся в `tests/test_job_applier.py` — проверить после правок.
