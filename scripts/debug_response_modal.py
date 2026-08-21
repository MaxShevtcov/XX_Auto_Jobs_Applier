"""Диагностика формы отклика hh.ru: почему не прикладывается сопроводительное письмо.

Скрипт открывает вакансию, жмёт "Откликнуться", дампит DOM модалки и инвентарь
data-qa атрибутов НИЧЕГО НЕ ОТПРАВЛЯЯ. Артефакты кладёт в logs/debug_response_modal/.

Использование:
    python scripts/debug_response_modal.py [URL_ВАКАНСИИ]
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.constants import SECRETS_FILE  # noqa: E402
from src.job_manager.playwright_manager import PlaywrightJobManager  # noqa: E402
from src.logger_config import logger  # noqa: E402
from src.utils.browser_utils import get_clean_text, safe_click, safe_fill  # noqa: E402
from src.utils.utils import load_yaml_file  # noqa: E402

DEFAULT_VACANCY_URL = ""


async def pick_active_vacancy(page) -> str:
    """Найти первую активную вакансию в поиске hh.ru."""
    logger.info("Ищем активную вакансию через поиск hh.ru")
    try:
        await page.goto(
            "https://hh.ru/search/vacancy?search_field=name&text=разработчик",
            timeout=60000,
            wait_until="domcontentloaded",
        )
    except Exception as e:
        logger.warning(f"Переход на поиск не удался ({e})")
        return ""
    await page.wait_for_timeout(3000)
    titles = page.locator('[data-qa="serp-item__title"]')
    cnt = await titles.count()
    for i in range(cnt):
        href = await titles.nth(i).get_attribute("href")
        if href:
            return href if href.startswith("http") else "https://hh.ru" + href
    return ""
OUT_DIR = os.path.join("logs", "debug_response_modal")

# селекторы, которые использует apply_to_vacancy в playwright_manager.py
CODE_SELECTORS = {
    "apply_btn_top": '[data-qa="vacancy-response-link-top"]',
    "apply_btn_bottom": '[data-qa="vacancy-response-link-bottom"]',
    "letter_informer_v1": '[data-qa="vacancy-response-letter-informer"]',
    "letter_informer_textarea_v1": '[data-qa="vacancy-response-letter-informer"] textarea[name="text"]',
    "letter_submit_v1": '[data-qa="vacancy-response-letter-submit"]',
    "letter_input_v2": '[data-qa="vacancy-response-popup-form-letter-input"]',
    "submit_popup_v2": '[data-qa="vacancy-response-submit-popup"]',
    "questions": '[data-qa="task-body"]',
    "resume_title_trigger": "[data-qa='resume-title']",
}

INTERESTING_QA_RE = re.compile(r"letter|response|submit|task|resume|cover", re.IGNORECASE)


async def dump_state(page, page_obj, tag: str) -> None:
    """Скриншот + HTML + инвентарь data-qa для текущего состояния страницы."""
    os.makedirs(OUT_DIR, exist_ok=True)
    png = os.path.join(OUT_DIR, f"{tag}.png")
    html = os.path.join(OUT_DIR, f"{tag}.html")
    await page.screenshot(path=png, full_page=False)
    with open(html, "w", encoding="utf-8") as f:
        f.write(await page.content())

    qas = await page_obj.evaluate(
        "() => Array.from(document.querySelectorAll('[data-qa]')).map(e => e.getAttribute('data-qa'))"
    )
    interesting = sorted({q for q in qas if q and INTERESTING_QA_RE.search(q)})
    logger.info(f"[{tag}] data-qa ({len(interesting)}): {interesting}")

    # проверка каждого селектора из боевого кода
    report = []
    for name, selector in CODE_SELECTORS.items():
        try:
            count = await page_obj.locator(selector).count()
        except Exception as e:
            count = f"ERR {e}"
        visible = None
        if isinstance(count, int) and count > 0:
            try:
                visible = await page_obj.locator(selector).first.is_visible()
            except Exception:
                visible = False
        report.append(f"  {name}: count={count}, visible={visible}")
    logger.info(f"[{tag}] селекторы боевого кода:\n" + "\n".join(report))


async def main() -> None:
    vacancy_url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_VACANCY_URL
    logger.info(f"Диагностика модалки отклика: {vacancy_url or '(автопоиск вакансии)'}")

    secrets = load_yaml_file(SECRETS_FILE)
    manager = PlaywrightJobManager(secrets)
    await manager.initialize()
    page = manager.page

    try:
        if not await manager.ensure_logged_in():
            logger.error("Не удалось авторизоваться (проверь логин/пароль или реши капчу в Telegram)")
            await dump_state(page, page, "00_login_failed")
            return

        if not vacancy_url:
            vacancy_url = await pick_active_vacancy(page)
            if not vacancy_url:
                logger.error("Не нашли ни одной активной вакансии в поиске")
                return
            logger.info(f"Тестовая вакансия: {vacancy_url}")

        try:
            await page.goto(vacancy_url, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            logger.warning(f"Первый переход не удался ({e}), пробуем ещё раз")
            await page.goto(vacancy_url, timeout=60000, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)

        if await page.locator('[data-qa="account-captcha-picture"]').count() > 0:
            logger.error("Обнаружена капча - продолжить диагностику нельзя")
            return

        # если вакансия неактивна - на странице не будет кнопки отклика;
        # пробуем взять живую вакансию из поиска
        if (
            await page.locator('[data-qa="vacancy-response-link-top"]').count() == 0
            and await page.locator('[data-qa="vacancy-response-link-bottom"]').count() == 0
        ):
            logger.warning("На этой странице нет кнопки отклика (вакансия неактивна?), берём живую из поиска")
            vacancy_url = await pick_active_vacancy(page)
            if not vacancy_url:
                logger.error("Не нашли ни одной активной вакансии в поиске")
                return
            logger.info(f"Тестовая вакансия: {vacancy_url}")
            await page.goto(vacancy_url, timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

        clicked = await safe_click(page, '[data-qa="vacancy-response-link-top"]', timeout=5000)
        if not clicked:
            clicked = await safe_click(page, '[data-qa="vacancy-response-link-bottom"]', timeout=5000)

        await page.wait_for_timeout(2500)

        # попап мог открыться даже при таймауте клика - проверяем факт
        popup_open = (
            await page.locator('[data-qa="response-popup-close"]').count() > 0
            or await page.locator('[data-qa="add-cover-letter"]').count() > 0
        )
        if not popup_open:
            logger.error("Модалка отклика не открылась (возможно, уже откликались)")
            await dump_state(page, page, "01_no_apply_button")
            return
        logger.info("Модалка отклика открыта")

        await dump_state(page, page, "01_after_apply_click")

        # если открылся выбор резюме - НЕ выбираем и НЕ сабмитим, просто фиксируем факт
        if await page.locator("[data-qa^='magritte-select-option-']").count() > 0:
            logger.warning(
                "Открылся выбор резюме! В боевом коде _select_resume после выбора "
                "сразу жмёт vacancy-response-submit-popup - это отправляет отклик БЕЗ письма"
            )

        # пробуем раскрыть блок письма реальной кнопкой hh.ru (не сабмит!)
        if await page.locator('[data-qa="add-cover-letter"]').count() > 0:
            logger.info("Найдена кнопка add-cover-letter - кликаем")
            await safe_click(page, '[data-qa="add-cover-letter"]', timeout=5000)
            await page.wait_for_timeout(1500)
            await dump_state(page, page, "02_after_add_letter_click")
        else:
            logger.warning("Кнопки add-cover-letter в модалке НЕТ")

        # контрольное заполнение письма (БЕЗ отправки)
        try:
            if await page.locator('[data-qa="vacancy-response-popup-form-letter-input"]').count() > 0:
                letter_text = "Тестовое сопроводительное письмо: диагностика, отклик не отправляется."
                filled = await safe_fill(
                    page, '[data-qa="vacancy-response-popup-form-letter-input"]', letter_text
                )
                await page.wait_for_timeout(800)
                value = (
                    await page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
                    .first.input_value(timeout=3000)
                )
                logger.info(
                    f"safe_fill={filled}, в поле {len(value or '')} символов, совпадение={value == letter_text}"
                )
                await dump_state(page, page, "03_after_letter_fill")
        except Exception as e:
            logger.warning(f"Блок заполнения письма пропущен: {e}")

        # дампим внутренности вопросов новой анкеты (для подбора селекторов)
        try:
            q_loc = page.locator("[data-qa^='vacancy-response-question']")
            q_cnt = await q_loc.count()
            logger.info(f"Вопросов нового формата: {q_cnt}")
            os.makedirs(OUT_DIR, exist_ok=True)
            for i in range(q_cnt):
                html = await q_loc.nth(i).evaluate("e => e.outerHTML")
                path = os.path.join(OUT_DIR, f"question_{i}.html")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(html)
                logger.info(f"Вопрос [{i}]: {len(html)} символов -> {path}")
        except Exception as e:
            logger.warning(f"Дамп вопросов не удался: {e}")

        # текстовые совпадения как в варианте 2 боевого кода
        cl_btn = page.locator("xpath=//*[text()='Добавить' or contains(text(), 'Сопроводительное')]")
        cnt = await cl_btn.count()
        logger.info(f"Вариант 2 (текст 'Добавить'/'Сопроводительное'): найдено {cnt} элементов")
        for i in range(min(cnt, 10)):
            el = cl_btn.nth(i)
            tag_name = await el.evaluate("e => e.tagName")
            txt = re.sub(r"\s+", " ", ((await el.text_content()) or ""))[:80]
            vis = await el.is_visible()
            logger.info(f"  [{i}] <{tag_name}> visible={vis} text='{txt}'")

        logger.info("Готово. Отклик НЕ отправлялся.")
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
