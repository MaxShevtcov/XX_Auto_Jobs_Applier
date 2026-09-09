import asyncio
import os
import random
import re
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from Levenshtein import distance
from playwright.async_api import Browser, BrowserContext, Page, Locator

from src.logger_config import logger
from src.telegram.telegram_manager import process_captcha
from src.utils.browser_utils import (
    create_playwright_browser,
    save_browser_session,
    safe_click,
    safe_fill,
    safe_goto,
    get_clean_text,
)
from src.utils.utils import sanitize_text
from src.views.resume import Resume


class PlaywrightJobManager:
    """
    Управляет экземпляром браузера Playwright, аутентификацией и высокоуровневыми взаимодействиями.
    """

    def __init__(self, secrets: dict):
        self.secrets = secrets
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.login = secrets.get("hh_login")
        self.password = secrets.get("hh_password")
        self.search_page_url = ""

    async def initialize(self):
        """Инициализирует браузер, контекст и страницу."""
        if not self.browser:
            self.browser, self.context, self.page = await create_playwright_browser()

    async def close(self):
        """Закрывает ресурсы браузера."""
        if self.context:
            await save_browser_session(self.context)
            await self.context.close()
            self.context = None
        if self.browser:
            await self.browser.close()
            self.browser = None
        self.page = None

    async def ensure_logged_in(self) -> bool:
        """Проверяет авторизацию, если нет - выполняет вход."""
        if not self.page:
            await self.initialize()
        logger.info("Проверяем статус авторизации...")
        await self.pause_async(2, 3)
        if not await self._is_logged_in():
            return await self._perform_login()
        return True

    async def _perform_login(self) -> bool:
        """Выполняет процесс входа."""
        # Сначала пробуем кликнуть по кнопке входа с достаточным таймаутом
        # (на медленной странице дефолтных 1 сек не хватает).
        if not await safe_click(self.page, "[data-qa*='login']", timeout=10000):
            # Фолбэк: сразу открываем страницу логина соискателя.
            logger.warning("Кнопка входа не найдена, переходим на страницу логина напрямую")
            try:
                await safe_goto(self.page, "https://hh.ru/account/login")
            except Exception as e:
                logger.error(f"Не удалось открыть страницу логина: {e}")
                return False

        # В некоторых случаях сначала появляется выбор типа аккаунта (работодатель/соискатель).
        # Всегда выбираем соискателя ("Я ищу работу").
        await self.pause_async(1, 2)
        logger.info("Обрабатываем выбор типа аккаунта")
        await self._handle_account_type_chooser_if_present()

        # HH может по умолчанию использовать телефон; переключаемся на email, если переключатель есть.
        logger.info("Выбираем тип учётных данных: email")
        await self._select_email_credential_type_if_present()

        # Сначала вводим логин (email) — HH может требовать это перед переходом к форме пароля
        await self.pause_async(1, 2)
        logger.info("Вводим логин")
        email_filled = await safe_fill(
            self.page,
            "//*[@data-qa='applicant-login-input-email']",
            self.login,
            wait_for_timeout=2000,
        )
        if not email_filled:
            # Фолбэк: универсальный селектор поля email
            email_filled = await safe_fill(
                self.page,
                "input[type='email']",
                self.login,
                timeout=5000,
            )
        if not email_filled:
            logger.error("Не удалось заполнить поле email на форме входа")
            return False

        # Открываем форму пароля (кнопка "Войти с паролем")
        logger.info("Открываем форму пароля")
        await safe_click(self.page, "//*[starts-with(@data-qa, 'expand-login-by')]")
        await self.pause_async(1, 2)

        # Вводим пароль
        logger.info("Вводим пароль")
        password_filled = await safe_fill(
            self.page,
            "//*[@data-qa='login-input-password' or @data-qa='applicant-login-input-password']",
            self.password,
            wait_for_timeout=10000,
        )
        if not password_filled:
            password_filled = await safe_fill(
                self.page,
                "input[type='password']",
                self.password,
                timeout=10000,
            )
        if not password_filled:
            logger.error("Не удалось заполнить поле пароля на форме входа")
            return False
        await self.pause_async(2, 3)

        # Нажимаем кнопку "Войти" (не нажимаем "Дальше" раньше времени)
        await safe_click(self.page, "//*[@data-qa='submit-button']", timeout=10000)
        await self.pause_async(2, 3)

        # Если после сабмита появилась капча — решаем через Telegram
        await self._handle_captcha(submit_selector="//*[@data-qa='submit-button']")

        # Проверяем наличие ошибок
        error_msg = self.page.locator("//*[@data-qa='account-login-error']")
        if await error_msg.count() > 0:
            text = await get_clean_text(error_msg.first)
            logger.error(f"Ошибка входа: {text}")
            return False

        # Проверяем успешность входа
        if await self._is_logged_in():
            logger.info("Вход выполнен успешно.")
            # Сохраняем сессию браузера после успешного входа
            await save_browser_session(self.context)
            return True
        else:
            logger.warning("Проверка входа не прошла.")
            return False

    async def _is_logged_in(self) -> bool:
        """Проверяет, выполнен ли вход."""
        logger.info("Переходим на страницу входа...")
        try:
            await safe_goto(self.page, "https://hh.ru/employer")
            logger.info("Переход на страницу: https://hh.ru/employer")
        except Exception as e:
            logger.warning(f"Не удалось перейти на страницу входа: {e}")
            logger.info("Пробуем продолжить...")

        try:
            resume_menu = self.page.locator('[data-qa="mainmenu_profileAndResumes"]')
            create_resume_button = self.page.locator('[data-qa="mainmenu_createResume"]')

            if await resume_menu.count() > 0 or await create_resume_button.count() > 0:
                logger.info("Пользователь уже авторизован.")
                return True
        except Exception as e:
            logger.warning(f"Ошибка при проверке статуса авторизации: {e}")
        return False

    async def _handle_account_type_chooser_if_present(self) -> None:
        """
        Обрабатывает выбор типа аккаунта (работодатель/соискатель), если он появляется.
        Если появляется, выбирает соискателя ("Я ищу работу") и нажимает "Войти".
        """
        if not self.page:
            return

        chooser_container = self.page.locator("//*[@data-qa='account-type-cards']")
        applicant_card = self.page.locator(
            "xpath=//*[contains(@data-qa, 'account-type-card-APPLICANT')]/ancestor::label[1]"
        )
        submit_btn = self.page.locator("//*[@data-qa='submit-button']")

        try:
            has_container = (await chooser_container.count()) > 0
            has_applicant = (await applicant_card.count()) > 0
            has_submit = (await submit_btn.count()) > 0
        except Exception as e:
            logger.warning(f"Ошибка при проверке выбора типа аккаунта: {e}")
            return

        if not (has_container or (has_applicant and has_submit)):
            return

        logger.info("Обнаружен выбор типа аккаунта. Выбираем аккаунт соискателя...")

        # Кликаем по карточке соискателя (по data-qa); откат на поиск по тексту.
        clicked = await safe_click(
            self.page,
            "//*[contains(@data-qa,'account-type-card-APPLICANT')]/ancestor::label[1]",
            timeout=10000,
        )
        if not clicked:
            await safe_click(
                self.page,
                "//*[.//span[@data-qa='cell-text-content' and contains(., 'Я') and contains(., 'ищу работу')]]",
                timeout=10000,
            )

        await safe_click(self.page, "//*[@data-qa='submit-button']", timeout=10000)
        await self.pause_async(1, 2)

    async def _select_email_credential_type_if_present(self) -> None:
        """
        Переключает тип входа на Email, если выбран телефон.
        Актуальная разметка HH: радио-кнопки [data-qa='credential-type-phone'] и
        [data-qa='credential-type-email'], выбранное состояние помечается словом
        'checked' в значении data-qa ("credential-type-phone checked").
        """
        if not self.page:
            return

        email_radio = self.page.locator("[data-qa='credential-type-email']")
        if (await email_radio.count()) == 0:
            logger.debug("Переключатель типа учётных данных не найден")
            return

        # Уже выбран email?
        qa_value = await email_radio.first.get_attribute("data-qa")
        if qa_value and "checked" in qa_value.lower():
            return

        logger.info("Обнаружен переключатель типа учётных данных. Переключаемся на EMAIL...")
        clicked = await safe_click(
            self.page,
            "//*[@data-qa='credential-type-email']/ancestor::label[1]",
            timeout=10000,
        )
        if not clicked:
            clicked = await safe_click(
                self.page,
                "[data-qa='credential-type-email']",
                timeout=10000,
            )
        if not clicked:
            try:
                await email_radio.first.check(timeout=10000)
            except Exception as e:
                logger.warning(f"Не удалось переключиться на EMAIL: {e}")
        await self.pause_async(0.5, 1)

    async def _handle_captcha(self, submit_selector: str):
        """Обрабатывает капчу, если она появляется."""
        captcha_img = self.page.locator("//*[@data-qa='account-captcha-picture']")

        start_time = datetime.now()

        while await captcha_img.count() > 0:
            if (datetime.now() - start_time).total_seconds() > 3600:
                logger.error("Капча не решена за 1 час.")
                break

            logger.info("Обнаружена капча.")

            img_path = "captcha_image.png"
            message_id = str(int(datetime.now().timestamp() * 10**6))

            # Отправляем капчу (или всегда свежий скриншот)
            try:
                await captcha_img.first.screenshot(path=img_path)
            except Exception as e:
                logger.error(f"Не удалось сохранить изображение капчи: {e}")
                break

            tg_token = self.secrets["tg_token"]
            tg_api_id = self.secrets.get("tg_api_id")
            tg_api_hash = self.secrets.get("tg_api_hash")
            tg_chat_id = self.secrets["tg_chat_id"]
            tg_topic_id = self.secrets["tg_captcha_topic_id"]

            # Отправляем изображение
            await process_captcha(
                tg_token,
                tg_api_id,
                tg_api_hash,
                tg_chat_id,
                tg_topic_id,
                img_path,
                message_id,
                listen=False,
            )

            # Ждём ответа
            answer = await process_captcha(
                tg_token,
                tg_api_id,
                tg_api_hash,
                tg_chat_id,
                tg_topic_id,
                img_path,
                message_id,
                listen=True,
            )

            if answer:
                logger.info(f"Получен ответ на капчу: {answer}")
                await safe_fill(self.page, "//*[@data-qa='account-captcha-input']", answer)
                await safe_click(self.page, submit_selector)

                # Ждём перезагрузки/проверки
                await self.pause_async(5, 6)
                if os.path.exists(img_path):
                    os.remove(img_path)
            else:
                await self.pause_async(5, 6)

    async def pause_async(self, low=0.5, high=1.0):
        """Асинхронная пауза. Время паузы выбирается случайно в пределах между low и high секунд."""
        await asyncio.sleep(random.uniform(low, high))

    async def start_search(self, resume_id: str) -> None:
        """Начинает поиск вакансий для указанного резюме."""
        url = f"https://hh.ru/resume/{resume_id}"
        await safe_goto(self.page, url)
        logger.info(f"Переход на страницу: {url}")
        await safe_click(self.page, "xpath=//*[contains(text(), 'Подобрали для вас')]")

    def _build_search_url(self, search_params: Dict[str, Any]) -> str:
        """Строит URL поиска hh.ru с query-параметрами из search_config."""
        params: Dict[str, Any] = {}

        # --- Ключевые слова ---
        keywords = str(search_params.get("keywords") or "").strip()
        words_to_exclude = str(search_params.get("words_to_exclude") or "").strip()
        if keywords or words_to_exclude:
            parts = []
            if keywords:
                parts.append(keywords)
            if words_to_exclude:
                parts.extend(f"-{w.strip()}" for w in words_to_exclude.split(",") if w.strip())
            params["text"] = " ".join(parts)

        # --- Где искать ---
        sf = search_params.get("search_field") or {}
        sf_keys = self._true_keys(sf)
        for key in sf_keys:
            # HH поддерживает несколько search_field. Раньше при выборе
            # названия и описания в URL уходило только описание.
            if key in {"name", "company_name", "description"}:
                params.setdefault("search_field", [])
                if isinstance(params["search_field"], str):
                    params["search_field"] = [params["search_field"]]
                params["search_field"].append(key)

        # --- Опыт ---
        exp = search_params.get("experience") or {}
        exp_key = self._first_true_key(exp)
        if exp_key:
            hh_value = "doesNotMatter" if exp_key == "doesntMatter" else exp_key
            params["experience"] = hh_value

        # --- Тип занятости ---
        emp = search_params.get("employment") or {}
        emp_keys = self._true_keys(emp)
        for k in emp_keys:
            if k == "INTERNSHIP":
                params.setdefault("label", [])
                if isinstance(params["label"], str):
                    params["label"] = [params["label"]]
                params["label"].append("internship")
            elif k == "ACCEPT_TEMPORARY":
                params.setdefault("label", [])
                if isinstance(params["label"], str):
                    params["label"] = [params["label"]]
                params["label"].append("accept_handicapped")
            else:
                params.setdefault("employment_form", [])
                if isinstance(params["employment_form"], str):
                    params["employment_form"] = [params["employment_form"]]
                params["employment_form"].append(k)

        # --- Формат работы ---
        jf = search_params.get("job_format") or {}
        jf_keys = self._true_keys(jf)
        for k in jf_keys:
            params.setdefault("work_format", [])
            if isinstance(params["work_format"], str):
                params["work_format"] = [params["work_format"]]
            params["work_format"].append(k)

        # --- Образование ---
        edu = search_params.get("education") or {}
        edu_map = {
            "not_needed": "not_required_or_not_specified",
            "middle": "special_secondary",
            "higher": "higher",
        }
        for k in self._true_keys(edu):
            suffix = edu_map.get(k)
            if suffix:
                params.setdefault("education", [])
                if isinstance(params["education"], str):
                    params["education"] = [params["education"]]
                params["education"].append(suffix)

        # --- Зарплата ---
        salary = search_params.get("salary")
        if salary is not None and salary != "":
            try:
                params["salary"] = str(int(salary))
            except (ValueError, TypeError):
                pass

        cur = search_params.get("currency") or {}
        cur_key = self._first_true_key(cur)
        if cur_key:
            params["currency_code"] = cur_key

        if search_params.get("only_with_salary") is True:
            params.setdefault("label", [])
            if isinstance(params["label"], str):
                params["label"] = [params["label"]]
            params["label"].append("with_salary")

        # --- Метки вакансий ---
        vl = search_params.get("vacancy_label") or {}
        for k in self._true_keys(vl):
            params.setdefault("label", [])
            if isinstance(params["label"], str):
                params["label"] = [params["label"]]
            params["label"].append(k)

        # --- Сортировка ---
        ob = search_params.get("order_by") or {}
        ob_key = self._first_true_key(ob)
        if ob_key and ob_key != "relevance":
            params["order_by"] = ob_key

        # --- Период ---
        period_map = {
            "all_time": "0",
            "month": "30",
            "week": "7",
            "three_days": "3",
            "one_day": "1",
        }
        per = search_params.get("period") or {}
        per_key = self._first_true_key(per)
        if per_key:
            days = period_map.get(per_key)
            if days and days != "0":
                params["search_period"] = days

        # --- Количество на странице ---
        show = search_params.get("show") or {}
        show_map = {"show_20": "20", "show_50": "50", "show_100": "100"}
        show_key = self._first_true_key(show)
        if show_key:
            count = show_map.get(show_key, "20")
            if count != "20":
                params["items_on_page"] = count

        # --- Регион (если передан числовой ID) ---
        area_raw = search_params.get("area") or ""
        area_str = str(area_raw).strip()
        if area_str:
            for a in self._split_multi(area_str):
                a = a.strip()
                if a.isdigit():
                    params.setdefault("area", [])
                    if isinstance(params["area"], str):
                        params["area"] = [params["area"]]
                    params["area"].append(a)

        query = urllib.parse.urlencode(params, doseq=True)
        return f"https://hh.ru/search/vacancy?{query}"

    async def set_advanced_search_params(
        self, search_params: Dict[str, Any], resume_id: str
    ) -> None:
        """
        Выполняет поиск вакансий на hh.ru с настройками из `search_config.yaml`.

        Основные фильтры применяются через URL query-параметры (надёжнее UI-кликов).
        Сложные фильтры (professional_role, industry, districts) требуют
        резолва имени → ID через UI-модалки и пока не поддерживаются через URL.
        """
        self.search_params = search_params or {}
        await self.start_search(resume_id)
        await self.pause_async(2, 3)

        search_url = self._build_search_url(self.search_params)
        logger.info(f"Переход на страницу поиска: {search_url}")
        await safe_goto(self.page, search_url)
        await self.pause_async(2, 3)
        await self._handle_interfering_messages()

    # -----------------------------
    # Вспомогательные методы расширенного поиска (UI)
    # -----------------------------

    @staticmethod
    def _split_multi(value: Any) -> List[str]:
        """Разделяет строку с несколькими значениями (через запятую или точку с запятой)."""
        if not value:
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if not isinstance(value, str):
            return [str(value).strip()] if str(value).strip() else []
        # Принимаем ввод, разделённый запятыми или точками с запятой
        raw = value.replace(";", ",")
        return [v.strip() for v in raw.split(",") if v.strip()]

    @staticmethod
    def _true_keys(value: Any) -> List[str]:
        """Возвращает список ключей словаря, значения которых True."""
        if not isinstance(value, dict):
            return []
        return [k for k, v in value.items() if v is True]

    @staticmethod
    def _first_true_key(value: Any) -> Optional[str]:
        """Возвращает первый ключ словаря с значением True."""
        keys = PlaywrightJobManager._true_keys(value)
        return keys[0] if keys else None

    async def _click_best_suggestion(self, desired: str, suggestion_xpath: str) -> bool:
        """Кликает по наилучшему предложению из выпадающего списка (на основе расстояния Левенштейна)."""
        desired_norm = (desired or "").strip().lower()
        if not desired_norm:
            return False
        suggestions = self.page.locator(suggestion_xpath)
        try:
            await suggestions.first.wait_for(state="visible", timeout=10000)
        except Exception:
            return False

        items = await suggestions.all()
        if not items:
            return False

        texts: List[str] = []
        for item in items:
            t = (await item.text_content()) or ""
            t = re.sub(r"\s+", " ", t).strip()
            texts.append(t)

        distances = [
            (idx, distance(desired_norm, (texts[idx] or "").lower())) for idx in range(len(texts))
        ]
        best_idx = min(distances, key=lambda x: x[1])[0]
        try:
            await safe_click(self.page, suggestion_xpath, element_number=best_idx)
            await self.pause_async(0.5, 1)
            return True
        except Exception:
            return False

    async def _set_keywords(self) -> None:
        """Устанавливает ключевые слова."""
        logger.debug("Вводим ключевые слова")
        keywords = self.search_params.get("keywords") or self.search_params.get("text") or ""
        keywords = str(keywords).strip()
        if not keywords:
            return
        # Новый интерфейс hh.ru использует общий поисковый инпут
        filled = await safe_fill(
            self.page, "[data-qa='search-input']", keywords, timeout=10000
        )
        if not filled:
            # Откат: старый инпут расширенного поиска
            await safe_fill(
                self.page,
                "[data-qa='vacancysearch__keywords-input']",
                keywords,
                timeout=10000,
            )
        suggestion_xpath = (
            "//*[@data-qa='suggest-item-cell' or @data-qa='suggester__keywords-item']"
        )
        await self._click_best_suggestion(keywords, f"xpath={suggestion_xpath}")
        await self.pause_async(0.5, 1)

    async def _set_search_field(self) -> None:
        """Задает настройки области поиска (в названии вакансии, компании, описании)."""
        logger.debug("Задаем настройки области поиска")
        search_field = self.search_params.get("search_field") or {}
        enabled = set(self._true_keys(search_field))
        if not enabled:
            return

        # Новый интерфейс hh.ru: радио-пары short_description (краткое описание -
        # название и компания) / full_description (полное описание).
        # Отдельной опции "только название" больше нет.
        if "name" in enabled or "company_name" in enabled:
            clicked = await safe_click(
                self.page,
                "xpath=//label[.//input[@data-qa='short_description']]",
                timeout=10000,
            )
            if not clicked:
                await safe_click(self.page, "[data-qa='short_description']", timeout=5000)

        if "description" in enabled:
            clicked = await safe_click(
                self.page,
                "xpath=//label[.//input[@data-qa='full_description']]",
                timeout=10000,
            )
            if not clicked:
                await safe_click(self.page, "[data-qa='full_description']", timeout=5000)
            return

        if not {"name", "company_name"} & enabled:
            return

        # Откат: старый интерфейс с чекбоксами name/company_name/description
        for key in ("name", "company_name", "description"):
            if key not in enabled:
                continue

            clicked = await safe_click(
                self.page,
                f"xpath=//label[.//input[@name='search_field' and @value='{key}']]",
                timeout=3000,
            )
            if not clicked:
                # Старый вариант отката: клик по видимому тексту
                text_map = {
                    "name": "в названии вакансии",
                    "company_name": "в названии компании",
                    "description": "в описании вакансии",
                }
                await safe_click(
                    self.page,
                    f"xpath=//*[self::label or self::span or self::div][contains(., '{text_map[key]}')]",
                    timeout=10000,
                )
            await self.pause_async(0.5, 1)

    async def _set_words_to_exclude(self) -> None:
        """Задает слова для исключения."""
        logger.debug("Задаем слова для исключения")
        words = self.search_params.get("words_to_exclude") or ""
        words = str(words).strip()
        if not words:
            return
        await safe_fill(
            self.page, "[data-qa='vacancysearch__keywords-excluded-input']", words, timeout=10000
        )
        await self.pause_async(0.5, 1)

    async def _set_tree_selector_single(self, open_text: str, value: str) -> None:
        """
        Выбирает одно значение в модальном окне с древовидным селектором (специализация/отрасль).
        Открывает модалку, вводит значение, выбирает лучшее совпадение, подтверждает.
        """
        value = str(value or "").strip()
        if not value:
            return

        # Открываем модальное окно
        opened = False
        for selector in (
            f"xpath=//*[normalize-space()='{open_text}']",
            f"xpath=//*[contains(., '{open_text}')]",
        ):
            if await safe_click(self.page, selector, timeout=10000):
                opened = True
                break
        if not opened:
            return

        await self.pause_async(0.5, 1)
        search_input_xpath = "//*[@data-qa='tree-selector-search-input' or @data-qa='bloko-tree-selector-popup-search']"
        await safe_fill(self.page, f"xpath={search_input_xpath}", value, timeout=10000)
        await self.pause_async(1, 2)

        # Подсказки внутри модального окна
        suggestion_xpath = (
            "//*[starts-with(@data-qa, 'tree-selector-item') "
            "or starts-with(@data-qa, 'bloko-tree-selector-item-text') "
            "or @data-qa='suggest-item-cell']"
        )

        picked = await self._click_best_suggestion(value, f"xpath={suggestion_xpath}")
        if not picked:
            # Закрываем/отменяем модальное окно, если ничего не найдено
            await safe_click(
                self.page,
                "xpath=//*[@data-qa='composite-selection-tree-selector-modal-cancel' or @data-qa='bloko-tree-selector-popup-cancel']",
                timeout=3000,
            )
            return

        await self.pause_async(0.5, 1)
        await safe_click(
            self.page,
            "xpath=//*[@data-qa='composite-selection-tree-selector-modal-submit' or @data-qa='bloko-tree-selector-popup-submit']",
            timeout=10000,
        )
        await self.pause_async(0.5, 1)

    async def _set_professional_role(self) -> None:
        """Задает профессиональную роль."""
        logger.debug("Задаем профессиональную роль")
        value = self.search_params.get("professional_role") or ""
        value = str(value).strip()
        if not value:
            return
        await self._set_tree_selector_single("Указать специализации", value)

    async def _set_industry(self) -> None:
        """Задает отрасль."""
        logger.debug("Задаем отрасль")
        value = self.search_params.get("industry") or ""
        value = str(value).strip()
        if not value:
            return
        await self._set_tree_selector_single("Указать отрасль компании", value)

    async def _set_area(self) -> None:
        """Задает регион."""
        logger.debug("Задаем регион")
        values = self._split_multi(self.search_params.get("area"))
        if not values:
            return

        input_selector = "[data-qa='advanced-search-region-add'] input"
        # В некоторых версиях HH используется нестандартный ввод без <input>
        if await self.page.locator(input_selector).count() == 0:
            input_selector = "[data-qa='advanced-search-region-add']"

        suggestion_xpath = (
            "//*[@data-qa='suggest-item-cell' or @data-qa='suggester__keywords-item']"
        )
        for region in values:
            if not region:
                continue
            if not await safe_fill(self.page, input_selector, region, timeout=10000):
                await safe_click(self.page, input_selector, timeout=10000)
                await self.page.keyboard.type(region)
            await self.pause_async(0.7, 1)
            await self._click_best_suggestion(region, f"xpath={suggestion_xpath}")

    async def _set_districts(self) -> None:
        """Задает районы."""
        logger.debug("Задаем районы")
        values = self._split_multi(self.search_params.get("districts"))
        if not values:
            return
        input_selector = "[data-qa='searchform__district-input']"
        if await self.page.locator(input_selector).count() == 0:
            return

        suggestion_xpath = (
            "//*[@data-qa='suggest-item-cell' or @data-qa='address-edit-district-suggest-item']"
        )
        for district in values:
            if not district:
                continue
            await safe_fill(self.page, input_selector, district, timeout=10000)
            await self.pause_async(0.7, 1)
            await self._click_best_suggestion(district, f"xpath={suggestion_xpath}")

    async def _set_salary_and_currency(self) -> None:
        """Задает зарплату и валюту."""
        logger.debug("Задаем зарплату и валюту")
        salary = self.search_params.get("salary")
        if salary is not None and salary != "":
            try:
                salary_val = str(int(salary))
            except Exception:
                salary_val = str(salary)
            await safe_fill(
                self.page, "[data-qa='advanced-search-salary']", salary_val, timeout=10000
            )
            await self.pause_async(0.5, 1)

        currency = self.search_params.get("currency") or {}
        currency_key = self._first_true_key(currency)
        if not currency_key:
            return

        # Новый интерфейс HH использует "chips" с радиокнопками: name="currency_code", data-qa="currency-code-RUR|EUR|USD"
        # Предпочтительно кликать по label с радиокнопкой (сами кнопки могут быть скрыты).
        clicked = await safe_click(
            self.page,
            f"xpath=//label[.//input[@name='currency_code' and (@value='{currency_key}' or @data-qa='currency-code-{currency_key}')]]",
            timeout=3000,
        )
        if clicked:
            await self.pause_async(0.5, 1)
            return

        # Откат: в некоторых старых версиях используется <select> или другой контейнер
        select_locator = self.page.locator(
            "select[name='currency'], [data-qa='advanced-search-currency'] select"
        )
        if await select_locator.count() > 0:
            try:
                await select_locator.first.select_option(currency_key)
                await self.pause_async(0.5, 1)
                return
            except Exception:
                pass

        text_map = {"RUR": "руб", "USD": "USD", "EUR": "EUR"}
        await safe_click(
            self.page,
            f"xpath=//*[self::label or self::span or self::div][contains(translate(., 'РУБUSDЕUR', 'рубusdеur'), '{text_map.get(currency_key, currency_key).lower()}')]",
            timeout=2000,
        )

    async def _set_only_with_salary(self) -> None:
        """Задает фильтр только с зарплатой."""
        logger.debug("Задаем фильтр только с зарплатой")
        only = self.search_params.get("only_with_salary")
        if only is not True:
            return
        # Новый интерфейс HH: чекбокс — input name="label" value="with_salary"
        if await safe_click(
            self.page,
            "xpath=//label[.//input[@name='label' and @value='with_salary']]",
            timeout=3000,
        ):
            await self.pause_async(0.5, 1)
            return

        # Откат: клик по тексту (старые версии)
        for t in (
            "Только с зарплатой",
            "Только с указанной зарплатой",
            "Только с указанием зарплаты",
            "Показывать только вакансии",
        ):
            if await safe_click(
                self.page,
                f"xpath=//*[self::label or self::span or self::div][contains(., '{t}')]",
                timeout=2000,
            ):
                await self.pause_async(0.5, 1)
                return

    async def _set_education(self) -> None:
        """Задает образование."""
        logger.debug("Задаем образование")
        edu = self.search_params.get("education") or {}
        mapping = {
            "not_needed": "not_required_or_not_specified",
            "middle": "special_secondary",
            "higher": "higher",
        }
        for key in self._true_keys(edu):
            suffix = mapping.get(key)
            if not suffix:
                continue
            await safe_click(
                self.page,
                f"[data-qa='advanced-search__education-item-label_{suffix}']",
                timeout=3000,
            )

    async def _set_experience(self) -> None:
        """Задает опыт работы."""
        logger.debug("Задаем опыт работы")
        exp = self.search_params.get("experience") or {}
        key = self._first_true_key(exp)
        if not key:
            return
        # В YAML используется doesntMatter, в HH — doesNotMatter
        if key == "doesntMatter":
            key = "doesNotMatter"
        await safe_click(
            self.page, f"[data-qa='advanced-search__experience-item-label_{key}']", timeout=3000
        )

    async def _set_employment(self) -> None:
        """Задает тип занятости."""
        logger.debug("Задаем тип занятости")
        employment = self.search_params.get("employment") or {}
        enabled = self._true_keys(employment)
        if not enabled:
            return

        for key in enabled:
            if key == "ACCEPT_TEMPORARY":
                await safe_click(
                    self.page,
                    "[data-qa='advanced-search__accept_temporary-item']",
                    timeout=3000,
                )
                await self.pause_async(0.5, 1)
                continue

            if key == "INTERNSHIP":
                await safe_click(
                    self.page,
                    "xpath=//label[.//input[@name='label' and @value='internship']]",
                    timeout=3000,
                )
                await self.pause_async(0.5, 1)
                continue

            await safe_click(
                self.page,
                f"xpath=//label[.//input[@name='employment_form' and @value='{key}']]",
                timeout=3000,
            )
            await self.pause_async(0.5, 1)

    async def _set_job_format(self) -> None:
        """Задает формат работы."""
        logger.debug("Задаем формат работы")
        job_format = self.search_params.get("job_format") or {}
        enabled = self._true_keys(job_format)
        if not enabled:
            return

        for key in enabled:
            if await safe_click(
                self.page,
                f"[data-qa='advanced-search__work_format-item-label_{key}']",
                timeout=1500,
            ):
                await self.pause_async(0.5, 1)
                continue

    async def _set_vacancy_label(self) -> None:
        """Задает метки вакансий."""
        logger.debug("Задаем метки вакансий")
        labels = self.search_params.get("vacancy_label") or {}
        for key in self._true_keys(labels):
            await safe_click(
                self.page, f"[data-qa='advanced-search__label-item-label_{key}']", timeout=3000
            )
            # advanced-search__label-item-label_accept_teens

    async def _set_order_by(self) -> None:
        """Задает сортировку."""
        logger.debug("Задаем сортировку")
        order_by = self.search_params.get("order_by") or {}
        key = self._first_true_key(order_by)
        if not key:
            return
        # Релевантность — обычно значение по умолчанию; всё равно кликаем, если пользователь указал.
        await safe_click(
            self.page, f"[data-qa='advanced-search__order_by-item-label_{key}']", timeout=3000
        )

    async def _set_period(self) -> None:
        """Задает период поиска."""
        logger.debug("Задаем период поиска")
        period = self.search_params.get("period") or {}
        key = self._first_true_key(period)
        if not key:
            return
        mapping = {
            "all_time": "0",
            "month": "30",
            "week": "7",
            "three_days": "3",
            "one_day": "1",
        }
        days = mapping.get(key)
        if days is None:
            return
        clicked = await safe_click(
            self.page, f"[data-qa='advanced-search__search_period-item-label_{days}']", timeout=3000
        )
        if not clicked:
            # Откат: поиск по тексту периода
            text_map = {"0": "За всё время", "30": "За месяц", "7": "За неделю", "3": "За 3 дня", "1": "За сутки"}
            text = text_map.get(days, "")
            if text:
                await safe_click(
                    self.page,
                    f"xpath=//label[contains(., '{text}')] | //*[contains(@data-qa,'search_period')][contains(., '{text}')]",
                    timeout=3000,
                )

    async def _set_show(self) -> None:
        """Задает количество вакансий, которые будут отображаться на одной странице."""
        logger.debug("Задаем количество вакансий, которые будут отображаться на одной странице")
        show = self.search_params.get("show") or {}
        key = self._first_true_key(show)
        key_mapping = {"show_20": "20", "show_50": "50", "show_100": "100"}
        if not key_mapping.get(key):
            return

        # Новый интерфейс Magritte
        await safe_click(
            self.page,
            f"[data-qa='advanced-search__items_on_page-item-label_{key_mapping[key]}']",
            timeout=3000,
        )

    async def get_vacancies_from_page(self, page_num: int = 0) -> List[Dict[str, Any]]:
        """Получить вакансии с очередной страницы."""
        # Логика пагинации: проверяем, находимся ли на запрошенной странице
        try:
            if not self.search_page_url:
                self.search_page_url = self.page.url

            if "hh.ru" in self.search_page_url:
                parsed = urllib.parse.urlparse(self.search_page_url)
                query = urllib.parse.parse_qs(parsed.query)
                current_page_param = query.get("page", ["0"])[0]

                if int(current_page_param) != page_num:
                    query["page"] = [str(page_num)]
                    new_query = urllib.parse.urlencode(query, doseq=True)
                    new_url = urllib.parse.urlunparse(parsed._replace(query=new_query))
                    self.search_page_url = new_url
                    logger.info(f"Переходим на страницу {page_num}: {new_url}")
                    await safe_goto(self.page, new_url)
                    await self.pause_async(2, 3)
        except Exception as e:
            logger.warning(f"Ошибка при обработке пагинации: {e}")

        vacancies = []
        # HH.ru ставит несколько значений через пробел (например, "vacancy-serp__vacancy vacancy-serp__vacancy_standard"),
        # поэтому exact CSS match не работает — используем XPath с проверкой по целому слову.
        card_xpath = "xpath=//*[contains(concat(' ', normalize-space(@data-qa), ' '), ' vacancy-serp__vacancy ')]"
        # Даём странице время отрендерить карточки
        try:
            await self.page.wait_for_selector(card_xpath, timeout=10000)
        except Exception:
            pass
        cards = await self.page.locator(card_xpath).all()

        logger.info(f"Найдено {len(cards)} вакансий на странице {page_num} (URL: {self.page.url})")

        for card in cards:
            vac = await self._parse_vacancy_card(card)
            if vac:
                vacancies.append(vac)

        return vacancies

    async def _parse_vacancy_card(self, card: Locator) -> Optional[Dict[str, Any]]:
        """Парсит карточку вакансии из поисковой выдачи."""
        try:
            # Элемент заголовка
            title_el = card.locator('[data-qa="serp-item__title"]').first
            if await title_el.count() == 0:
                return None

            title = await get_clean_text(title_el)
            href = await title_el.get_attribute("href")
            if not href:
                return None

            # Полный URL
            if not href.startswith("http"):
                full_url = "https://hh.ru" + href
            else:
                full_url = href

            # ID вакансии
            vacancy_id = None
            # ID обычно находится в пути URL /vacancy/123456
            match = re.search(r"vacancy/(\d+)", full_url)
            if match:
                vacancy_id = match.group(1)

            # Работодатель
            employer_name = "Неизвестно"
            employer_id = None
            emp_el = card.locator('[data-qa="vacancy-serp__vacancy-employer"]').first
            if await emp_el.count() > 0:
                employer_name = await get_clean_text(emp_el)
                emp_href = await emp_el.get_attribute("href")
                if emp_href:
                    match_emp = re.search(r"employer/(\d+)", emp_href)
                    if match_emp:
                        employer_id = match_emp.group(1)

            return {
                "name": title,
                "id": vacancy_id,
                "alternate_url": full_url,
                "employer": {
                    "id": employer_id,
                    "name": employer_name,
                },
            }
        except Exception as e:
            logger.warning(f"Не удалось распарсить карточку вакансии: {e}")
            return None

    async def get_vacancy_full_info(self, vacancy_url: str) -> Dict[str, Any]:
        """Получает полную информацию о вакансии для LLM."""
        await safe_goto(self.page, vacancy_url)
        logger.info(f"Переход на страницу: {vacancy_url}")

        async def get_text_or_empty(selector: str) -> str:
            el = self.page.locator(selector)
            if await el.count() > 0:
                return await get_clean_text(el.first)
            return ""

        description = await get_text_or_empty('[data-qa="vacancy-description"]')
        title = await get_text_or_empty('[data-qa="vacancy-title"]')

        # Навыки
        skills_list = []
        skills_els = self.page.locator('[data-qa="skills-element"]')
        count = await skills_els.count()
        for i in range(count):
            skills_list.append(await get_clean_text(skills_els.nth(i)))
        skills = ", ".join(skills_list)

        # Детали заголовка
        experience = await get_text_or_empty('[data-qa="work-experience-text"]')
        employment = await get_text_or_empty('[data-qa="common-employment-text"]')
        hiring_formats = await get_text_or_empty('[data-qa="vacancy-hiring-formats"]')
        schedule = await get_text_or_empty('[data-qa="work-schedule-by-days-text"]')
        working_hours = await get_text_or_empty('[data-qa="working-hours-text"]')
        work_formats = await get_text_or_empty('[data-qa="work-formats-text"]')

        # Зарплата
        salary = await get_text_or_empty('[data-qa="vacancy-salary"]')
        if not salary:
            # Откат по структуре сниппета (Magritte)
            salary = await get_text_or_empty("xpath=//div[contains(@class, 'vacancy-title')]/span")

        return {
            "title": title,
            "salary": salary,
            "experience": experience,
            "employment": employment,
            "hiring_formats": hiring_formats,
            "schedule": schedule,
            "working_hours": working_hours,
            "work_formats": work_formats,
            "description": description,
            "skills": skills,
        }

    async def _handle_interfering_messages(self) -> None:
        """Обрабатывает мешающие сообщения (куки, уведомления, попапы)."""
        message_was_processed = True

        while message_was_processed:
            await self.pause_async(2, 3)

            # Куки
            cookies_btn = self.page.locator("xpath=//*[text()='Понятно']")
            if await cookies_btn.count() > 0:
                await cookies_btn.click()
                break
            # Уведомления
            close_btn = self.page.locator('[data-qa="notification-close-button"]')
            if await close_btn.count() > 0:
                await close_btn.click()
                break
            # Попап сбора дополнительных данных
            save_btn = self.page.locator('[data-qa="additional-data-collector__popup-save"]')
            if await save_btn.count() > 0:
                await save_btn.click()
                break

            message_was_processed = False

    # Селекторы, по наличию хотя бы одного из которых понимаем, что форма отклика открылась
    _RESPONSE_FORM_SELECTORS = (
        '[data-qa="vacancy-response-submit-popup"]',
        '[data-qa="vacancy-response-letter-informer"]',
        '[data-qa="add-cover-letter"]',
        '[data-qa="vacancy-response-popup-form-letter-input"]',
        "[data-qa='resume-title']",
    )

    async def _is_response_form_opened(self) -> bool:
        """Проверяет, открылась ли реально форма отклика (модалка или страница отклика)."""
        combined_selector = ", ".join(self._RESPONSE_FORM_SELECTORS)
        try:
            await self.page.locator(combined_selector).first.wait_for(
                state="attached", timeout=5000
            )
            return True
        except Exception:
            pass
        # hh.ru иногда не открывает модалку, а редиректит на страницу отклика
        return "vacancy_response" in (self.page.url or "")

    async def apply_to_vacancy(
        self, vacancy_url: str, cover_letter: str, gpt_answerer: Any, resume_component: Any
    ) -> Tuple[str, str]:
        """
        Откликается на вакансию с повторными попытками. Возвращает (Результат, Сообщение).
        Результат: 'Success', 'Skip', 'Error', 'Limit'
        """
        max_attempts = 3
        result, msg = "Error", "Неизвестная ошибка"
        for attempt in range(1, max_attempts + 1):
            try:
                result, msg = await self._apply_to_vacancy_attempt(
                    vacancy_url,
                    cover_letter,
                    gpt_answerer,
                    resume_component,
                    force_reload=attempt > 1,
                )
            except Exception as e:
                logger.error(f"Ошибка при отклике (попытка {attempt}/{max_attempts}): {e}")
                result, msg = "Error", str(e)
            if result != "Error" or attempt == max_attempts:
                return result, msg
            logger.warning(f"Попытка {attempt}/{max_attempts} не удалась: {msg}. Повторяем")
            await self.pause_async(2, 4)

        return result, msg

    async def _apply_to_vacancy_attempt(
        self,
        vacancy_url: str,
        cover_letter: str,
        gpt_answerer: Any,
        resume_component: Any,
        force_reload: bool = False,
    ) -> Tuple[str, str]:
        """Одна попытка отклика на вакансию."""
        if force_reload or self.page.url != vacancy_url:
            await safe_goto(self.page, vacancy_url)
            logger.info(f"Переход на страницу: {vacancy_url}")

        await self.pause_async(1, 2)

        # Нажимаем "Откликнуться"
        apply_btn_top_selector = '[data-qa="vacancy-response-link-top"]'
        apply_btn_bottom_selector = '[data-qa="vacancy-response-link-bottom"]'

        logger.info("Жмем кнопку 'Откликнуться'")
        clicked = await safe_click(self.page, apply_btn_top_selector, timeout=10000, click_all=True)
        if not clicked:
            clicked = await safe_click(
                self.page, apply_btn_bottom_selector, timeout=10000, click_all=True
            )

        # Клик мог сработать, даже если safe_click вернул False: Playwright считает
        # ошибкой таймаут «waiting for scheduled navigations» уже ПОСЛЕ выполненного клика.
        # Поэтому проверяем фактическое состояние страницы, а не только результат клика.
        form_opened = await self._is_response_form_opened()

        # Фолбэк: JS-клик напрямую, если форма не открылась
        # (обычный клик блокируется перекрытием или анимациями)
        if not form_opened:
            logger.warning("Обычный клик по кнопке отклика не прошел, пробуем клик через JS")
            js_clicked = await self._js_click(apply_btn_top_selector) or await self._js_click(
                apply_btn_bottom_selector
            )
            if js_clicked:
                form_opened = await self._is_response_form_opened()

        if not form_opened:
            reason = (
                "Кнопка отклика не найдена"
                if not clicked
                else "Форма отклика не открылась после нажатия кнопки"
            )
            return "Error", reason

        await self.pause_async(1, 2)

        await self._select_resume(resume_component)
        logger.info("Выбрали резюме")

        # Ждём модального окна или перехода
        await self._handle_interfering_messages()

        # Обрабатываем вопросы
        questions_selector = '[data-qa="task-body"]'
        questions_count = await self.page.locator(questions_selector).count()
        if questions_count > 0:
            logger.info(f"Найдено {questions_count} вопросов")
            for i in range(questions_count):
                question_xpath = f"(//*[@data-qa='task-body'])[{i + 1}]"
                question_locator = self.page.locator(question_xpath)
                success, msg = await self._handle_question(
                    question_locator,
                    gpt_answerer,
                    resume_component,
                    question_selector=question_xpath,
                )
                if not success:
                    return "Skip", msg

        # Сопроводительное письмо вариант 1
        magritte_cl_form = self.page.locator('[data-qa="vacancy-response-letter-informer"]')
        if await magritte_cl_form.count() > 0:
            logger.info("Найдена форма сопроводительного письма (вариант 1)")
            await safe_fill(
                self.page,
                '[data-qa="vacancy-response-letter-informer"] textarea[name="text"]',
                cover_letter,
            )
            await self.pause_async(1, 2)
            # Отправляем сопроводительное письмо
            logger.info("Жмем кнопку отправки сопроводительного письма (вариант 1)")
            if await safe_click(
                self.page, '[data-qa="vacancy-response-letter-submit"]', timeout=10000
            ):
                await self.pause_async(2, 3)
                return "Success", "Сопроводительное письмо отправлено"

        # Сопроводительное письмо вариант 2
        # В текущем интерфейсе hh.ru форма письма скрыта и раскрывается кнопкой
        # add-cover-letter; textarea vacancy-response-popup-form-letter-input
        # появляется только после её нажатия.
        add_letter_selector = '[data-qa="add-cover-letter"]'
        add_letter_btn = self.page.locator(add_letter_selector)
        if await add_letter_btn.count() > 0 and await add_letter_btn.first.is_visible():
            logger.info("Жмем кнопку 'Добавить сопроводительное письмо' (add-cover-letter)")
            await safe_click(self.page, add_letter_selector, timeout=5000)
            await self.pause_async(1, 2)
        else:
            # Откат для старых версий интерфейса hh.ru
            cl_btn_xpath = "xpath=//*[text()='Добавить' or contains(text(), 'Сопроводительное')]"
            if await self.page.locator(cl_btn_xpath).count() > 0:
                if await self.page.locator(cl_btn_xpath).first.is_visible():
                    logger.info("Жмем кнопку открытия формы сопроводительного письма (вариант 2)")
                    await safe_click(self.page, cl_btn_xpath, supress_warnings=True)
                    await self.pause_async(1, 2)

        letter_filled = not cover_letter
        cl_input = self.page.locator('[data-qa="vacancy-response-popup-form-letter-input"]')
        if await cl_input.count() > 0:
            logger.info("Заполняем форму сопроводительного письма (вариант 2)")
            letter_filled = await safe_fill(
                self.page,
                '[data-qa="vacancy-response-popup-form-letter-input"]',
                cover_letter,
                supress_warnings=True,
            )
            await self.pause_async(1, 2)

        await self._handle_interfering_messages()

        # Если письмо сгенерировано, но заполнить его не удалось - НЕ отправляем отклик,
        # иначе он уйдет без сопроводительного письма
        if not letter_filled:
            return "Error", "Не удалось заполнить сопроводительное письмо"

        # Проверяем, открыто ли окно с кнопкой отправки
        modal_submit_btn = self.page.locator('[data-qa="vacancy-response-submit-popup"]')
        if await modal_submit_btn.count() > 0 and await modal_submit_btn.is_visible():
            logger.info("Жмем кнопку отправки сопроводительного письма (вариант 2)")
            await safe_click(self.page, '[data-qa="vacancy-response-submit-popup"]')
            await self.pause_async(3, 4)
            return "Success", "Отклик отправлен"

        # Жмем кнопку 'Откликнуться'
        submit_btn = self.page.locator("xpath=//*[text()='Откликнуться']")
        if await submit_btn.count() > 0:
            logger.info("Жмем кнопку 'Откликнуться'")
            await safe_click(self.page, submit_btn)
            await self.pause_async(3, 4)
            return "Success", ""

        return "Error", "Кнопка отправки не найдена"

    async def _js_click(self, selector: str) -> bool:
        """Кликает по первому элементу через JS, когда обычный клик не проходит.

        Работает только с CSS-селекторами (xpath не поддерживается querySelector).
        """
        if "xpath=" in selector:
            return False
        try:
            handle = await self.page.evaluate_handle(
                "(selector) => document.querySelector(selector)", selector
            )
            element = handle.as_element()
            if element is None:
                return False
            await element.evaluate("(el) => el.click()")
            logger.info(f"Клик через JS выполнен: {selector}")
            return True
        except Exception as e:
            logger.warning(f"JS-клик не удался для '{selector}': {e}")
            return False

    async def _select_resume(self, resume_component: Any) -> None:
        """Выбирает резюме из списка."""
        if not self.page or not resume_component:
            return

        target_title = (getattr(resume_component, "job_title", "") or "").strip()
        if not target_title:
            return

        # Триггер выбора резюме присутствует только в некоторых сценариях отклика.
        trigger_selectors = [
            "[data-qa='resume-title']",
            "xpath=//*[@data-qa='resume-title']",
        ]
        trigger_clicked = False
        for selector in trigger_selectors:
            try:
                if await safe_click(self.page, selector, timeout=2000):
                    trigger_clicked = True
                    break
            except Exception:
                continue

        if trigger_clicked:
            await self.pause_async(0.5, 1)

        # Список вариантов отображается как magritte select list
        options_locator = self.page.locator("[data-qa^='magritte-select-option-']")
        try:
            await options_locator.first.wait_for(state="visible", timeout=3000)
        except Exception:
            return

        options = await options_locator.all()
        if not options:
            return

        titles: List[str] = []
        for opt in options:
            title_el = opt.locator("[data-qa='resume-title'] [data-qa='cell-text-content']").first
            if await title_el.count() == 0:
                title_el = opt.locator("[data-qa='resume-title']").first
            title_text = (await title_el.text_content()) if await title_el.count() > 0 else ""
            title_text = re.sub(r"\s+", " ", (title_text or "")).strip()
            titles.append(title_text)

        # Выбираем наиболее близкое название
        target_norm = target_title.lower()
        best_idx = None
        best_dist = None
        for idx, title in enumerate(titles):
            if not title:
                continue
            d = distance(target_norm, title.lower())
            if best_dist is None or d < best_dist:
                best_dist = d
                best_idx = idx

        if best_idx is None:
            return

        logger.info(f"Выбрано резюме для отклика: {titles[best_idx]}")

        await safe_click(self.page, "[data-qa^='magritte-select-option-']", element_number=best_idx)

        # ВАЖНО: не отправляем отклик здесь - сначала основной флоу должен
        # обработать вопросы и сопроводительное письмо, иначе отклик уйдет без письма
        await self.pause_async(0.5, 1)

    async def _handle_question(
        self,
        question: Locator,
        gpt_answerer: Any,
        resume_component: Any,
        question_selector: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """Обрабатывает одиночный вопрос в анкете."""
        # 1. Извлекаем текст вопроса
        question_text_el = question.locator('[data-qa="task-question"]').first
        if await question_text_el.count() > 0:
            question_text = await get_clean_text(question_text_el)
        else:
            question_text = await get_clean_text(question)
        logger.info(f"Обрабатываем вопрос: {question_text}")

        # 2. Проверяем ячейки Magritte (Radio/Checkbox с метками)
        cells = await question.locator('[data-qa="cell"]').all()
        if cells:
            # Определяем тип по первой ячейке
            is_radio = await cells[0].locator('[data-qa="radio-container"]').count() > 0
            is_checkbox = await cells[0].locator('[data-qa="checkbox-container"]').count() > 0

            if is_radio or is_checkbox:
                options = []
                for cell in cells:
                    text_el = cell.locator('[data-qa="cell-text-content"]').first
                    text = await get_clean_text(text_el) if await text_el.count() > 0 else ""
                    options.append(text)

                options.append("Нет информации")

                if is_radio:
                    answer = gpt_answerer.select_one_answer_from_options(question_text, options)
                    clicked = False
                    for i, opt in enumerate(options):
                        if opt == answer and opt != "Нет информации":
                            await cells[i].click()
                            clicked = True
                            break
                    if clicked:
                        return True, ""
                    return False, "Подходящий ответ не найден"

                elif is_checkbox:
                    answers = gpt_answerer.select_many_answers_from_options(question_text, options)
                    clicked = False
                    for i, opt in enumerate(options):
                        if opt in answers and opt != "Нет информации":
                            await cells[i].click()
                            clicked = True
                    return clicked, "Подходящий ответ не найден" if not clicked else ""

        # 3. Radio (откат)
        radios = await question.locator('[data-qa="radio-container"]').all()
        if radios:
            options = []
            for r in radios:
                options.append(await get_clean_text(r))

            options.append("Нет информации")
            answer = gpt_answerer.select_one_answer_from_options(question_text, options)

            for i, opt in enumerate(options):
                if opt == answer and opt != "Нет информации":
                    if question_selector:
                        # Используем safe_click с индексом относительно вопроса
                        radio_xpath = f"{question_selector}//*[@data-qa='radio-container']"
                        await safe_click(self.page, radio_xpath, element_number=i)
                    else:
                        # Откат, если не передан селектор (не должно происходить при новом вызове)
                        await radios[i].click()
                    return True, ""
            return False, "Подходящий ответ не найден"

        # 4. Checkbox (откат)
        checkboxes = await question.locator('[data-qa="checkbox-container"]').all()
        if checkboxes:
            options = []
            for c in checkboxes:
                options.append(await get_clean_text(c))

            options.append("Нет информации")
            answers = gpt_answerer.select_many_answers_from_options(question_text, options)

            clicked = False
            for i, opt in enumerate(options):
                if opt in answers and opt != "Нет информации":
                    if question_selector:
                        checkbox_xpath = f"{question_selector}//*[@data-qa='checkbox-container']"
                        await safe_click(self.page, checkbox_xpath, element_number=i)
                    else:
                        await checkboxes[i].click()
                    clicked = True

            return clicked, "Подходящий ответ не найден" if not clicked else ""

        # 5. Textarea (внутри)
        textarea = question.locator("textarea")
        if await textarea.count() > 0:
            answer = gpt_answerer.answer_question_textual_wide_range(question_text)
            answer = resume_component.deanonymize_personal_information(answer)
            await textarea.fill(answer)
            return True, ""

        # 6. Textarea (соседний элемент - Magritte)
        if question_selector:
            sibling_textarea = self.page.locator(
                f"xpath={question_selector}/following-sibling::div[@data-qa='textarea-wrapper'][1]//textarea"
            )
            if await sibling_textarea.count() > 0:
                answer = gpt_answerer.answer_question_textual_wide_range(question_text)
                await sibling_textarea.fill(answer)
                return True, ""

        return False, "Неизвестный тип вопроса"

    async def get_my_resumes_from_browser(self) -> Dict[str, Any]:
        """Получает список резюме пользователя через браузер."""
        await self.ensure_logged_in()
        # Открываем страницу "Резюме и профиль" из главного меню
        menu_selector = '[data-qa="mainmenu_profileAndResumes"]'
        clicked = await safe_click(self.page, menu_selector, timeout=10000)
        if not clicked:
            await safe_goto(self.page, "https://hh.ru")
            logger.info("Переход на страницу: https://hh.ru")
            await self.pause_async(1, 2)
            await safe_click(self.page, menu_selector, timeout=10000)
        # Ждём появления карточек резюме на странице
        try:
            await self.page.wait_for_selector(
                'a[data-qa^="resume-card-link-"][href*="/resume/"]',
                timeout=15000,
            )
        except Exception:
            logger.warning("Список резюме не найден после открытия страницы 'Резюме и профиль'.")
            return {"items": []}

        def _extract_resume_id_from_href(href: Optional[str]) -> Optional[str]:
            if not href:
                return None
            match = re.search(r"/resume/([a-zA-Z0-9]+)", href)
            if match:
                return match.group(1)
            match = re.search(r"[?&]resume=([a-zA-Z0-9]+)", href)
            if match:
                return match.group(1)
            return None

        resumes: List[Dict[str, Any]] = []
        seen_ids: set[str] = set()

        links = await self.page.locator('a[data-qa^="resume-card-link-"][href*="/resume/"]').all()
        for link in links:
            title_el = link.locator('[data-qa="resume-title"] [data-qa="cell-text-content"]').first
            title = ((await title_el.text_content()) or "").strip()
            if not title:
                title_el = link.locator('[data-qa="resume-title"]').first
                title = ((await title_el.text_content()) or "").strip()

            href = await link.get_attribute("href")
            resume_id = _extract_resume_id_from_href(href)
            if not resume_id:
                continue

            if resume_id in seen_ids:
                continue
            seen_ids.add(resume_id)

            resumes.append({"id": resume_id, "title": title})

        logger.info(f"Найдено {len(resumes)} резюме")

        return {"items": resumes}

    async def get_resume_content_from_browser(self, resume_id: str) -> Dict[str, Any]:
        """
        Открывает страницу резюме hh.ru и парсит ключевые разделы.

        Сохраняет обратную совместимость, возвращая данные в формате API, когда это возможно,
        и всегда добавляет спарсенные разделы.
        """
        resume = {}

        user_profile_url = "https://hh.ru/profile/me"
        await safe_goto(self.page, user_profile_url)
        logger.info(f"Переход на страницу: {user_profile_url}")
        await self.pause_async(3, 4)

        resume["personal_information"] = {}
        resume["personal_information"]["first_name"] = await self._get_first_name()
        resume["personal_information"]["last_name"] = await self._get_last_name()
        resume["personal_information"]["birthday"] = await self._get_birthday()
        resume["personal_information"]["telegram"] = await self._get_telegram()
        resume["personal_information"]["whatsapp"] = await self._get_whatsapp()
        resume["area"] = await self._get_area()
        resume["driving_license"] = await self._get_driving_license()
        linkedin, habr_career = await self._get_other_links()
        if linkedin:
            resume["personal_information"]["linkedin"] = linkedin
        if habr_career:
            resume["personal_information"]["habr_career"] = habr_career
        await safe_click(
            self.page,
            '[data-qa="profile-common-card-edit"]',
            timeout=10000,
        )
        await self.pause_async(2, 3)
        middle_name = await self._get_middle_name()
        birthday = await self._get_birthday()

        if middle_name:
            resume["personal_information"]["middle_name"] = middle_name
        if birthday:
            resume["personal_information"]["birthday"] = birthday
        (
            sex,
            citizenship,
            legal_auth,
        ) = await self._get_sex_citizenship_and_legal_auth()
        if sex:
            resume["personal_information"]["sex"] = sex
        if citizenship:
            resume["citizenship"] = citizenship
        if legal_auth:
            resume["legal_authorization"] = legal_auth

        resume_url = f"https://hh.ru/resume/{resume_id}"
        await safe_goto(self.page, resume_url)
        logger.info(f"Переход на страницу: {resume_url}")
        await self.pause_async(2, 3)

        resume["personal_information"]["phone"] = await self._get_resume_phone()
        resume["personal_information"]["email"] = await self._get_resume_email()
        resume["job_preferences"] = {}
        resume["job_preferences"]["job_type"] = await self._get_job_type()
        resume["job_preferences"]["job_format"] = await self._get_job_format()
        resume["job_preferences"]["time_to_travel"] = await self._get_time_to_travel()
        resume["job_preferences"][
            "readiness_to_job_trips"
        ] = await self._get_readiness_to_job_trips()
        resume["job_preferences"]["salary"] = await self._get_salary()
        resume["total_experience"] = await self._get_total_experience()
        resume["experience"] = await self._get_experience()
        resume["skills"] = await self._get_skills()
        resume["educations"] = await self._get_educations()
        resume["recommendations"] = await self._get_recommendations()
        resume["additional_education"] = await self._get_additional_education()
        resume["exams"] = await self._get_exams()
        resume["certificates"] = await self._get_certificates()
        await self.raise_resume()

        resume_url = f"https://hh.ru/resume/edit/{resume_id}/about"
        await safe_goto(self.page, resume_url)
        logger.info(f"Переход на страницу: {resume_url}")
        await self.pause_async(2, 3)
        resume["about_me"] = await self._get_about_me()
        return Resume(**resume).model_dump()

    async def raise_resume(self) -> None:
        """Поднимает резюме в поиске."""
        raise_btn_xpath = "xpath=//*[contains(text(), 'Поднять в') and contains(text(), 'поиске')]"
        if await safe_click(self.page, raise_btn_xpath):
            await self.pause_async(2, 3)
            logger.info("Резюме успешно поднято")
        else:
            logger.info("Резюме пока нельзя поднять")

    async def _get_first_name(self) -> str:
        """Получает имя из профиля."""
        first_name = ""
        el = self.page.locator('[data-qa="profile-common-card-firstname"]')
        if await el.count() > 0:
            first_name = sanitize_text((await el.first.text_content()) or "", lowercase=False)
        return first_name

    async def _get_other_links(self) -> Tuple[str, str]:
        """Получает другие ссылки (LinkedIn, Habr Career)."""
        linkedin = ""
        habr_career = ""
        other_links = await self.page.locator(
            '[data-qa*="profile-other-communication-methods-card-row"]'
        ).all()
        for other_link in other_links:
            text = await other_link.text_content()
            text = text.replace("\u2009", "").replace("\xa0", " ")
            if "linkedin.com" in text:
                linkedin = text
            elif "habr.ru" in text:
                habr_career = text
        return linkedin, habr_career

    async def _get_middle_name(self) -> str:
        """Получает отчество из профиля."""
        middle_name = self.page.locator('[data-qa*="profile-common-edit-middleName"]')
        if await middle_name.count() == 0:
            return ""
        middle_name = await middle_name.first.get_attribute("value")
        middle_name = sanitize_text(middle_name, lowercase=False)
        return middle_name

    async def _get_birthday(self) -> str:
        """Получает дату рождения."""
        birthday = self.page.locator('[data-qa="profile-common-edit-birthday"]')
        if await birthday.count() == 0:
            return ""
        birthday = await birthday.first.get_attribute("value")
        birthday = sanitize_text(birthday)
        return birthday

    async def _get_sex_citizenship_and_legal_auth(self) -> Tuple[str, str, str]:
        """Получает пол, гражданство и разрешение на работу."""
        select_activators = await self.page.locator('[data-qa="magritte-select-activator"]').all()
        sex = ""
        citizenship = ""
        work_permission = ""
        for select_activator in select_activators:
            text = await select_activator.text_content()
            text = text.replace("\u2009", "").replace("\xa0", " ")
            if text.startswith("Пол"):
                sex = text.replace("Пол", "").strip()
            elif text.startswith("Гражданство"):
                citizenship = text.replace("Гражданство", "").strip()
            elif text.startswith("Разрешение на работу"):
                work_permission = text.replace("Разрешение на работу", "").strip()
        return sex, citizenship, work_permission

    async def _get_last_name(self) -> str:
        """Получает фамилию."""
        last_name = ""
        el = self.page.locator('[data-qa="profile-common-card-lastname"]')
        if await el.count() > 0:
            last_name = sanitize_text((await el.first.text_content()) or "", lowercase=False)
        return last_name

    async def _get_telegram(self) -> str:
        """Получает Telegram из контактов."""
        telegram = self.page.locator("xpath=//*[contains(text(), 'Telegram')]")
        if await telegram.count() == 0:
            return ""
        parent = telegram.first.locator("../../../../../../../..")
        telegram = await parent.text_content()
        telegram = telegram.replace("Telegram", "").strip()
        return telegram

    async def _get_whatsapp(self) -> str:
        """Получает WhatsApp из контактов."""
        whatsapp = self.page.locator("xpath=//*[contains(text(), 'Whatsapp')]")
        if await whatsapp.count() == 0:
            return ""
        parent = whatsapp.first.locator("../../../../../../../..")
        whatsapp = await parent.text_content()
        whatsapp = whatsapp.replace("Whatsapp", "").strip()
        return whatsapp

    async def _get_area(self) -> str:
        """Получает местоположение (город)."""
        area = self.page.locator("xpath=//*[contains(text(), 'Где живёте')]")
        if await area.count() == 0:
            return ""
        parent = area.first.locator("../../../../../../../..")
        area = await parent.text_content()
        area = area.replace("Где живёте", "").strip()
        area = area.split("·")[0].strip()
        return area

    async def _get_driving_license(self) -> str:
        """Получает информацию о водительских правах."""
        driving_license = self.page.locator("xpath=//*[contains(text(), 'Опыт вождения')]")
        if await driving_license.count() == 0:
            return ""
        parent = driving_license.first.locator("../../..")
        driving_license = await parent.text_content()
        driving_license = driving_license.replace("Опыт вождения", "").strip()
        driving_license = sanitize_text(driving_license)
        driving_license = driving_license.split("·")[0].strip()
        return driving_license

    async def _get_resume_phone(self) -> str:
        """Получает телефон из резюме."""
        phone = self.page.locator(
            '[data-qa="resume-contact-phone-value-text"], [data-qa="resume-contact-phone-value-preferred-text"]'
        )
        if await phone.count() == 0:
            return ""
        phone = await phone.first.text_content()
        phone = sanitize_text(phone)
        return phone

    async def _get_resume_email(self) -> str:
        """Получает email из резюме."""
        email = self.page.locator(
            '[data-qa="resume-contact-email-value-text"], [data-qa="resume-contact-email-value-preferred-text"]'
        )
        if await email.count() == 0:
            return ""
        email = await email.first.text_content()
        email = sanitize_text(email, lowercase=False)
        return email

    async def _get_salary(self) -> str:
        """Получает зарплату из резюме."""
        salary = self.page.locator('[data-qa="title-description"]')
        if await salary.count() == 0:
            return ""
        salary = await salary.text_content()
        salary = sanitize_text(salary)
        return salary

    async def _get_job_type(self) -> str:
        """Получает тип занятости из резюме."""
        job_type = self.page.locator("xpath=//*[contains(text(), 'Тип занятости:')]")
        if await job_type.count() == 0:
            return ""
        parent = job_type.first.locator("..")
        job_type = await parent.text_content()
        job_type = sanitize_text(job_type)
        job_type = job_type.split(":")[1].strip()
        return job_type

    async def _get_job_format(self) -> str:
        """Получает формат работы из резюме."""
        job_format = self.page.locator("xpath=//*[contains(text(), 'Формат работы:')]")
        if await job_format.count() == 0:
            return ""
        parent = job_format.first.locator("..")
        job_format = await parent.text_content()
        job_format = sanitize_text(job_format)
        job_format = job_format.split(":")[1].strip()
        return job_format

    async def _get_time_to_travel(self) -> str:
        """Получает желательное время в пути до работы."""
        time_to_travel = self.page.locator("xpath=//*[contains(text(), 'Желательное время')]")
        if await time_to_travel.count() == 0:
            return ""
        parent = time_to_travel.first.locator("..")
        time_to_travel = await parent.text_content()
        time_to_travel = sanitize_text(time_to_travel)
        time_to_travel = time_to_travel.split(":")[1].strip()
        return time_to_travel

    async def _get_readiness_to_job_trips(self) -> str:
        """Получает готовность к командировкам."""
        ready_to_job_trip = self.page.locator("xpath=//*[contains(text(), 'Командировки:')]")
        if await ready_to_job_trip.count() == 0:
            return ""
        parent = ready_to_job_trip.first.locator("..")
        ready_to_job_trip = await parent.text_content()
        ready_to_job_trip = sanitize_text(ready_to_job_trip)
        ready_to_job_trip = ready_to_job_trip.split(":")[1].strip()
        return ready_to_job_trip

    async def _get_total_experience(self) -> str:
        """Получает общий опыт работы."""
        total_experience = self.page.locator("xpath=//*[contains(text(), 'Опыт работы:')]")
        if await total_experience.count() == 0:
            return ""
        parent = total_experience.first.locator("..")
        total_experience = await parent.text_content()
        total_experience = sanitize_text(total_experience)
        total_experience = total_experience.split(":")[1].strip()
        return total_experience

    async def _get_experience(self) -> str:
        """Получает опыт работы (описание)."""
        experience = self.page.locator('[data-qa="resume-list-card-experience"]')
        group_locators = experience.locator('[class^="group--"]')
        experience_texts = await group_locators.all_text_contents()
        experience_texts = [
            text.replace("\u2009", "").replace("\xa0", " ") for text in experience_texts
        ]
        experience = "\n".join(experience_texts)
        return experience

    async def _get_skills(self) -> str:
        """Получает навыки."""
        skill_card = self.page.locator("[data-qa='skills-card']")
        skills = skill_card.locator('[class^="magritte-tag__label"]')
        skills = await skills.all_text_contents()
        skills = "\n".join(skills)
        return skills

    async def _get_educations(self) -> str:
        """Получает образование."""
        education_card = self.page.locator("[data-qa='resume-list-card-education']")
        educations = education_card.locator('[data-qa="cell-text-content"]')
        educations = await educations.all_text_contents()
        educations = "\n".join(educations)
        return educations

    async def _get_about_me(self) -> str:
        """Получает информацию 'Обо мне'."""
        about_me = self.page.locator("[data-qa='resume-editor-about']")
        about_me = await about_me.all_text_contents()
        about_me = "\n".join(about_me)
        return about_me

    async def _get_recommendations(self) -> str:
        """Получает рекомендации."""
        recommendations = self.page.locator("[data-qa='resume-list-card-recommendation']")
        recommendations_locator = recommendations.locator('[data-qa="cell-text-content"]')
        recommendations = await recommendations_locator.all_text_contents()
        recommendations = "\n".join(recommendations)
        return recommendations

    async def _get_additional_education(self) -> str:
        """Получает дополнительное образование."""
        additional_education = self.page.locator("[data-qa='resume-list-card-additionalEducation']")
        additional_education_locator = additional_education.locator('[data-qa="cell-text-content"]')
        additional_education = await additional_education_locator.all_text_contents()
        additional_education = "\n".join(additional_education)
        return additional_education

    async def _get_exams(self) -> str:
        """Получает информацию об экзаменах/тестах."""
        exams = self.page.locator("[data-qa='resume-list-card-certificate']")
        exams_locator = exams.locator('[data-qa="cell-text-content"]')
        exams = await exams_locator.all_text_contents()
        exams = [
            r
            for r in exams
            if not (r == "Профориентация" or r.startswith("Тест поможет определить ваши"))
        ]
        exams = "\n".join(exams)
        return exams

    async def _get_certificates(self) -> str:
        """Получает сертификаты."""
        certificates = self.page.locator("[data-qa='resume-list-card-certificate']")
        certificates_locator = certificates.locator('[data-qa="cell-text-content"]')
        certificates = await certificates_locator.all_text_contents()
        certificates = "\n".join(certificates)
        return certificates
