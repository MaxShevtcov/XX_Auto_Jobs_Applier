from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.job_manager.playwright_manager import PlaywrightJobManager


SECRETS = {
    "hh_login": "test@example.com",
    "hh_password": "secret",
    "tg_token": "token",
    "tg_api_id": "api_id",
    "tg_api_hash": "api_hash",
    "tg_chat_id": "chat_id",
    "tg_captcha_topic_id": "topic_id",
}


@pytest.fixture
def manager():
    return PlaywrightJobManager(SECRETS)


@pytest.fixture
def manager_with_page(manager):
    manager.page = MagicMock()
    manager.browser = MagicMock()
    manager.context = MagicMock()
    return manager


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init(manager):
    assert manager.login == "test@example.com"
    assert manager.password == "secret"
    assert manager.browser is None
    assert manager.context is None
    assert manager.page is None
    assert manager.search_page_url == ""


# ---------------------------------------------------------------------------
# Статические вспомогательные методы
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, []),
        ("", []),
        ("Python, Django, Flask", ["Python", "Django", "Flask"]),
        ("Python; Django; Flask", ["Python", "Django", "Flask"]),
        ("  Python ,  Django  ", ["Python", "Django"]),
        (["Python", "Django"], ["Python", "Django"]),
        (["Python", "", "Django"], ["Python", "Django"]),
        (42, ["42"]),
        (0, []),
    ],
)
def test_split_multi(value, expected):
    assert PlaywrightJobManager._split_multi(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"a": True, "b": False, "c": True}, ["a", "c"]),
        ({"a": False}, []),
        ({}, []),
        ("not a dict", []),
        (None, []),
        (42, []),
    ],
)
def test_true_keys(value, expected):
    assert PlaywrightJobManager._true_keys(value) == expected


@pytest.mark.parametrize(
    "value, expected",
    [
        ({"a": True, "b": False}, "a"),
        ({"a": False, "b": True}, "b"),
        ({"a": False}, None),
        ({}, None),
        (None, None),
    ],
)
def test_first_true_key(value, expected):
    assert PlaywrightJobManager._first_true_key(value) == expected


# ---------------------------------------------------------------------------
# pause_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pause_async(manager):
    with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        await manager.pause_async(0.1, 0.2)
        mock_sleep.assert_awaited_once()
        elapsed = mock_sleep.call_args[0][0]
        assert 0.1 <= elapsed <= 0.2


# ---------------------------------------------------------------------------
# initialize / close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_initialize_creates_browser(manager):
    mock_browser = MagicMock()
    mock_context = MagicMock()
    mock_page = MagicMock()

    with patch(
        "src.job_manager.playwright_manager.create_playwright_browser",
        new_callable=AsyncMock,
        return_value=(mock_browser, mock_context, mock_page),
    ):
        await manager.initialize()

    assert manager.browser is mock_browser
    assert manager.context is mock_context
    assert manager.page is mock_page


@pytest.mark.asyncio
async def test_initialize_skips_if_browser_exists(manager_with_page):
    existing_browser = manager_with_page.browser

    with patch(
        "src.job_manager.playwright_manager.create_playwright_browser",
        new_callable=AsyncMock,
    ) as mock_create:
        await manager_with_page.initialize()
        mock_create.assert_not_awaited()

    assert manager_with_page.browser is existing_browser


@pytest.mark.asyncio
async def test_close_releases_resources(manager_with_page):
    mock_context_close = AsyncMock()
    mock_browser_close = AsyncMock()
    manager_with_page.context.close = mock_context_close
    manager_with_page.browser.close = mock_browser_close

    with patch(
        "src.job_manager.playwright_manager.save_browser_session",
        new_callable=AsyncMock,
    ):
        await manager_with_page.close()

    mock_context_close.assert_awaited_once()
    mock_browser_close.assert_awaited_once()
    assert manager_with_page.context is None
    assert manager_with_page.browser is None
    assert manager_with_page.page is None


@pytest.mark.asyncio
async def test_close_with_no_resources(manager):
    """close() без браузера/контекста не должен бросать исключений."""
    await manager.close()


# ---------------------------------------------------------------------------
# ensure_logged_in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_logged_in_already_logged_in(manager_with_page):
    with (
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch.object(manager_with_page, "_is_logged_in", new_callable=AsyncMock, return_value=True),
        patch.object(manager_with_page, "_perform_login", new_callable=AsyncMock) as mock_login,
    ):
        result = await manager_with_page.ensure_logged_in()

    assert result is True
    mock_login.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_logged_in_calls_perform_login(manager_with_page):
    with (
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch.object(
            manager_with_page, "_is_logged_in", new_callable=AsyncMock, return_value=False
        ),
        patch.object(
            manager_with_page, "_perform_login", new_callable=AsyncMock, return_value=True
        ) as mock_login,
    ):
        result = await manager_with_page.ensure_logged_in()

    assert result is True
    mock_login.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_logged_in_initializes_if_no_page(manager):
    assert manager.page is None

    with (
        patch.object(manager, "initialize", new_callable=AsyncMock) as mock_init,
        patch.object(manager, "pause_async", new_callable=AsyncMock),
        patch.object(manager, "_is_logged_in", new_callable=AsyncMock, return_value=True),
    ):
        await manager.ensure_logged_in()

    mock_init.assert_awaited_once()


# ---------------------------------------------------------------------------
# get_vacancies_from_page
# ---------------------------------------------------------------------------


@pytest.fixture
def _make_vacancy_card():
    """Фабрика заглушек карточек вакансий."""

    def _make(title: str, href: str, employer_name: str, employer_href: str):
        card = MagicMock()

        title_el = MagicMock()
        title_el.count = AsyncMock(return_value=1)
        title_el.get_attribute = AsyncMock(return_value=href)

        emp_el = MagicMock()
        emp_el.count = AsyncMock(return_value=1)
        emp_el.get_attribute = AsyncMock(return_value=employer_href)

        def locator_side_effect(selector):
            mock = MagicMock()
            if "serp-item__title" in selector:
                mock.first = title_el
            elif "vacancy-serp__vacancy-employer" in selector:
                mock.first = emp_el
            return mock

        card.locator = locator_side_effect

        return card, title, employer_name

    return _make


@pytest.mark.asyncio
async def test_get_vacancies_from_page_returns_list(manager_with_page, _make_vacancy_card):
    card, title, employer = _make_vacancy_card(
        "Python Developer",
        "https://hh.ru/vacancy/123456",
        "Test Corp",
        "https://hh.ru/employer/654321",
    )

    manager_with_page.page.url = "https://hh.ru/search/vacancy?page=0"
    manager_with_page.search_page_url = "https://hh.ru/search/vacancy?page=0"

    cards_locator = MagicMock()
    cards_locator.all = AsyncMock(return_value=[card])
    manager_with_page.page.locator = MagicMock(return_value=cards_locator)

    with (
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch(
            "src.job_manager.playwright_manager.get_clean_text",
            new_callable=AsyncMock,
            side_effect=[title, employer],
        ),
    ):
        vacancies = await manager_with_page.get_vacancies_from_page(page_num=0)

    assert len(vacancies) == 1
    assert vacancies[0]["name"] == "Python Developer"
    assert vacancies[0]["id"] == "123456"
    assert vacancies[0]["alternate_url"] == "https://hh.ru/vacancy/123456"
    assert vacancies[0]["employer"]["id"] == "654321"
    assert vacancies[0]["employer"]["name"] == "Test Corp"


@pytest.mark.asyncio
async def test_get_vacancies_from_page_navigates_on_page_mismatch(manager_with_page):
    manager_with_page.search_page_url = "https://hh.ru/search/vacancy?page=0"
    manager_with_page.page.goto = AsyncMock()

    cards_locator = MagicMock()
    cards_locator.all = AsyncMock(return_value=[])
    manager_with_page.page.locator = MagicMock(return_value=cards_locator)

    with patch.object(manager_with_page, "pause_async", new_callable=AsyncMock):
        await manager_with_page.get_vacancies_from_page(page_num=2)

    manager_with_page.page.goto.assert_awaited_once()
    called_url = manager_with_page.page.goto.call_args[0][0]
    assert "page=2" in called_url


# ---------------------------------------------------------------------------
# _parse_vacancy_card
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_vacancy_card_success(manager_with_page):
    card = MagicMock()

    title_el = MagicMock()
    title_el.count = AsyncMock(return_value=1)
    title_el.get_attribute = AsyncMock(return_value="https://hh.ru/vacancy/111222")

    emp_el = MagicMock()
    emp_el.count = AsyncMock(return_value=1)
    emp_el.get_attribute = AsyncMock(return_value="https://hh.ru/employer/999")

    def _locator(selector):
        mock = MagicMock()
        if "serp-item__title" in selector:
            mock.first = title_el
        elif "vacancy-serp__vacancy-employer" in selector:
            mock.first = emp_el
        return mock

    card.locator = _locator

    with patch(
        "src.job_manager.playwright_manager.get_clean_text",
        new_callable=AsyncMock,
        side_effect=["Senior Python Developer", "Acme Inc"],
    ):
        result = await manager_with_page._parse_vacancy_card(card)

    assert result is not None
    assert result["name"] == "Senior Python Developer"
    assert result["id"] == "111222"
    assert result["alternate_url"] == "https://hh.ru/vacancy/111222"
    assert result["employer"]["id"] == "999"
    assert result["employer"]["name"] == "Acme Inc"


@pytest.mark.asyncio
async def test_parse_vacancy_card_no_title_returns_none(manager_with_page):
    card = MagicMock()

    title_el = MagicMock()
    title_el.count = AsyncMock(return_value=0)

    locator_mock = MagicMock()
    locator_mock.first = title_el
    card.locator = MagicMock(return_value=locator_mock)

    result = await manager_with_page._parse_vacancy_card(card)

    assert result is None


@pytest.mark.asyncio
async def test_parse_vacancy_card_no_href_returns_none(manager_with_page):
    card = MagicMock()

    title_el = MagicMock()
    title_el.count = AsyncMock(return_value=1)
    title_el.get_attribute = AsyncMock(return_value=None)

    locator_mock = MagicMock()
    locator_mock.first = title_el
    card.locator = MagicMock(return_value=locator_mock)

    with patch(
        "src.job_manager.playwright_manager.get_clean_text",
        new_callable=AsyncMock,
        return_value="Some Title",
    ):
        result = await manager_with_page._parse_vacancy_card(card)

    assert result is None


@pytest.mark.asyncio
async def test_parse_vacancy_card_relative_href(manager_with_page):
    """Относительный href должен дополняться до полного URL."""
    card = MagicMock()

    title_el = MagicMock()
    title_el.count = AsyncMock(return_value=1)
    title_el.get_attribute = AsyncMock(return_value="/vacancy/777888")

    emp_el = MagicMock()
    emp_el.count = AsyncMock(return_value=0)

    def _locator(selector):
        mock = MagicMock()
        if "serp-item__title" in selector:
            mock.first = title_el
        else:
            mock.first = emp_el
        return mock

    card.locator = _locator

    with patch(
        "src.job_manager.playwright_manager.get_clean_text",
        new_callable=AsyncMock,
        return_value="Dev",
    ):
        result = await manager_with_page._parse_vacancy_card(card)

    assert result is not None
    assert result["alternate_url"] == "https://hh.ru/vacancy/777888"


# ---------------------------------------------------------------------------
# _is_logged_in
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_logged_in_true_when_menu_present(manager_with_page):
    manager_with_page.page.goto = AsyncMock()

    resume_menu = MagicMock()
    resume_menu.count = AsyncMock(return_value=1)

    create_resume = MagicMock()
    create_resume.count = AsyncMock(return_value=0)

    def _locator(selector):
        if "profileAndResumes" in selector:
            return resume_menu
        return create_resume

    manager_with_page.page.locator = _locator

    result = await manager_with_page._is_logged_in()
    assert result is True


@pytest.mark.asyncio
async def test_is_logged_in_false_when_no_menu(manager_with_page):
    manager_with_page.page.goto = AsyncMock()

    no_element = MagicMock()
    no_element.count = AsyncMock(return_value=0)
    manager_with_page.page.locator = MagicMock(return_value=no_element)

    result = await manager_with_page._is_logged_in()
    assert result is False


# ---------------------------------------------------------------------------
# _handle_interfering_messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_interfering_messages_no_popups(manager_with_page):
    """Без попапов — цикл завершается без кликов."""
    no_element = MagicMock()
    no_element.count = AsyncMock(return_value=0)
    manager_with_page.page.locator = MagicMock(return_value=no_element)

    with patch.object(manager_with_page, "pause_async", new_callable=AsyncMock):
        await manager_with_page._handle_interfering_messages()

    manager_with_page.page.locator.assert_called()


@pytest.mark.asyncio
async def test_handle_interfering_messages_closes_cookies(manager_with_page):
    cookies_btn = MagicMock()
    cookies_btn.count = AsyncMock(return_value=1)
    cookies_btn.click = AsyncMock()

    no_element = MagicMock()
    no_element.count = AsyncMock(return_value=0)

    def _locator(selector):
        if "Понятно" in selector:
            return cookies_btn
        return no_element

    manager_with_page.page.locator = _locator

    with patch.object(manager_with_page, "pause_async", new_callable=AsyncMock):
        await manager_with_page._handle_interfering_messages()

    cookies_btn.click.assert_awaited_once()


# ---------------------------------------------------------------------------
# apply_to_vacancy / _select_resume
# ---------------------------------------------------------------------------


def _make_locator(count=0, visible=True, text=""):
    loc = MagicMock()
    loc.count = AsyncMock(return_value=count)
    loc.is_visible = AsyncMock(return_value=visible)
    if text:
        loc.text_content = AsyncMock(return_value=text)
    # как у реального Locator: .first возвращает локатор с тем же API
    loc.first = loc
    return loc


def _route_locators(page, routes):
    """Маршрутизатор page.locator: точное совпадение селектора -> мок."""

    def _locate(selector):
        for key, value in routes.items():
            if key in selector:
                return value
        return _make_locator(0)

    page.locator = MagicMock(side_effect=_locate)


@pytest.mark.asyncio
async def test_apply_to_vacancy_opens_letter_form_before_submit(manager_with_page):
    """
    В новом интерфейсе hh.ru форма письма раскрывается кнопкой add-cover-letter.
    Код обязан кликнуть её и заполнить textarea ДО нажатия vacancy-response-submit-popup.
    """
    page = manager_with_page.page
    page.url = "https://hh.ru/vacancy/1"
    letter_input = _make_locator(count=1)
    _route_locators(
        page,
        {
            "vacancy-response-link-top": _make_locator(count=1),
            "task-body": _make_locator(0),
            "vacancy-response-letter-informer": _make_locator(0),
            "add-cover-letter": _make_locator(count=1),
            "vacancy-response-popup-form-letter-input": letter_input,
            "vacancy-response-submit-popup": _make_locator(count=1),
        },
    )

    calls = []

    async def fake_click(_page, selector, **_kwargs):
        calls.append(("click", selector))
        return True

    async def fake_fill(_page, selector, _text, **_kwargs):
        calls.append(("fill", selector))
        return True

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=fake_click),
        patch("src.job_manager.playwright_manager.safe_fill", side_effect=fake_fill),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch.object(
            manager_with_page, "_is_response_form_opened", new_callable=AsyncMock, return_value=True
        ),
    ):
        result, reason = await manager_with_page.apply_to_vacancy(
            "https://hh.ru/vacancy/1", "Тестовое письмо", None, None
        )

    assert result == "Success"
    selectors = [s for _, s in calls]

    def call_idx(fragment):
        return next(i for i, s in enumerate(selectors) if fragment in s)

    assert any("add-cover-letter" in s for s in selectors)
    assert any("vacancy-response-popup-form-letter-input" in s for s in selectors)
    assert any("vacancy-response-submit-popup" in s for s in selectors)
    # письмо раскрывается и заполняется ДО отправки отклика
    assert call_idx("add-cover-letter") < call_idx("vacancy-response-popup-form-letter-input")
    assert call_idx("vacancy-response-popup-form-letter-input") < call_idx(
        "vacancy-response-submit-popup"
    )


@pytest.mark.asyncio
async def test_apply_to_vacancy_fails_if_letter_not_filled(manager_with_page):
    """Если заполнить письмо не удалось — отклик НЕ отправляется."""
    page = manager_with_page.page
    page.url = "https://hh.ru/vacancy/1"
    _route_locators(
        page,
        {
            "vacancy-response-link-top": _make_locator(count=1),
            "task-body": _make_locator(0),
            "vacancy-response-letter-informer": _make_locator(0),
            "add-cover-letter": _make_locator(count=1),
            "vacancy-response-popup-form-letter-input": _make_locator(count=1),
            "vacancy-response-submit-popup": _make_locator(count=1),
        },
    )

    async def fake_click(_page, selector, **_kwargs):
        return True

    async def failing_fill(_page, _selector, _text, **_kwargs):
        return False

    submitted = []

    async def tracking_click(_page, selector, **_kwargs):
        if "submit" in selector:
            submitted.append(selector)
        return True

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=tracking_click),
        patch("src.job_manager.playwright_manager.safe_fill", side_effect=failing_fill),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch("src.job_manager.playwright_manager.safe_goto", new_callable=AsyncMock),
        patch.object(
            manager_with_page, "_is_response_form_opened", new_callable=AsyncMock, return_value=True
        ),
    ):
        result, reason = await manager_with_page.apply_to_vacancy(
            "https://hh.ru/vacancy/1", "Тестовое письмо", None, None
        )

    assert result == "Error"
    assert "сопроводительное" in reason.lower()
    assert not submitted


@pytest.mark.asyncio
@pytest.mark.parametrize("has_cover_letter", [False])
async def test_apply_to_vacancy_without_letter_submits(manager_with_page, has_cover_letter):
    """Без текста письма (пустой cover_letter) отклик отправляется как раньше."""
    page = manager_with_page.page
    page.url = "https://hh.ru/vacancy/1"
    _route_locators(
        page,
        {
            "vacancy-response-link-top": _make_locator(count=1),
            "task-body": _make_locator(0),
            "vacancy-response-letter-informer": _make_locator(0),
            "add-cover-letter": _make_locator(0),
            "vacancy-response-submit-popup": _make_locator(count=1),
        },
    )

    async def fake_click(_page, _selector, **_kwargs):
        return True

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=fake_click),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch.object(
            manager_with_page, "_is_response_form_opened", new_callable=AsyncMock, return_value=True
        ),
    ):
        result, _ = await manager_with_page.apply_to_vacancy(
            "https://hh.ru/vacancy/1", "", None, None
        )

    assert result == "Success"


@pytest.mark.asyncio
async def test_apply_to_vacancy_js_fallback_when_response_click_fails(manager_with_page):
    """Если обычный клик по кнопке отклика не прошел, должен срабатывать JS-фолбэк."""
    page = manager_with_page.page
    page.url = "https://hh.ru/vacancy/1"
    _route_locators(
        page,
        {
            "task-body": _make_locator(0),
            "vacancy-response-letter-informer": _make_locator(0),
            "add-cover-letter": _make_locator(0),
            "vacancy-response-submit-popup": _make_locator(count=1),
        },
    )

    clicked_selectors = []

    async def failing_for_response_links_click(_page, selector, **kwargs):
        if "vacancy-response-link" in selector:
            assert kwargs.get("timeout") == 10000
            return False
        clicked_selectors.append(selector)
        return True

    element = MagicMock()
    element.evaluate = AsyncMock(return_value=None)
    handle = MagicMock()
    handle.as_element.return_value = element

    async def fake_evaluate_handle(_script, _selector):
        return handle

    page.evaluate_handle = AsyncMock(side_effect=fake_evaluate_handle)

    # форма открывается только после JS-клика
    form_opened_mock = AsyncMock(side_effect=[False, True])

    with (
        patch(
            "src.job_manager.playwright_manager.safe_click",
            side_effect=failing_for_response_links_click,
        ),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch.object(manager_with_page, "_is_response_form_opened", form_opened_mock),
    ):
        result, reason = await manager_with_page.apply_to_vacancy(
            "https://hh.ru/vacancy/1", "", None, None
        )

    assert result == "Success"
    assert reason == "Отклик отправлен"


@pytest.mark.asyncio
async def test_apply_to_vacancy_error_when_all_clicks_fail(manager_with_page):
    """Если и обычный клик, и JS-фолбэк не удались - отклик завершается ошибкой."""
    page = manager_with_page.page
    page.url = "https://hh.ru/vacancy/1"

    handle = MagicMock()
    handle.as_element.return_value = None
    page.evaluate_handle = AsyncMock(return_value=handle)

    async def all_clicks_fail(_page, _selector, **_kwargs):
        return False

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=all_clicks_fail),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
        patch("src.job_manager.playwright_manager.safe_goto", new_callable=AsyncMock),
        patch.object(
            manager_with_page,
            "_is_response_form_opened",
            new_callable=AsyncMock,
            return_value=False,
        ),
    ):
        result, reason = await manager_with_page.apply_to_vacancy(
            "https://hh.ru/vacancy/1", "", None, None
        )

    assert result == "Error"
    assert reason == "Кнопка отклика не найдена"


@pytest.mark.asyncio
async def test_select_resume_does_not_submit_popup(manager_with_page):
    """Выбор резюме не должен отправлять отклик — это делает основной флоу после письма."""
    page = manager_with_page.page

    option = MagicMock()
    cell = MagicMock()
    cell.count = AsyncMock(return_value=1)
    cell.text_content = AsyncMock(return_value="Android разработчик")
    title_holder = MagicMock()
    title_holder.first = cell
    option.locator = MagicMock(return_value=title_holder)

    options_locator = MagicMock()
    options_locator.first.wait_for = AsyncMock()
    options_locator.all = AsyncMock(return_value=[option])

    def _locate(selector):
        if "magritte-select-option-" in selector:
            return options_locator
        return _make_locator(count=0)

    page.locator = MagicMock(side_effect=_locate)

    clicked = []

    async def fake_click(_page, selector, **_kwargs):
        clicked.append(selector)
        return True

    resume_component = MagicMock()
    resume_component.job_title = "Android разработчик"

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=fake_click),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
    ):
        await manager_with_page._select_resume(resume_component)

    assert any("magritte-select-option-" in s for s in clicked)
    assert not any("vacancy-response-submit-popup" in s for s in clicked)


# ---------------------------------------------------------------------------
# _get_first_name / _get_last_name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_first_name_returns_empty_when_element_missing(manager_with_page):
    """Если карточки имени нет на странице — возвращается пустая строка, а не Locator."""
    no_element = MagicMock()
    no_element.count = AsyncMock(return_value=0)
    manager_with_page.page.locator = MagicMock(return_value=no_element)

    assert await manager_with_page._get_first_name() == ""
    assert await manager_with_page._get_last_name() == ""


@pytest.mark.asyncio
async def test_get_first_name_returns_text_when_present(manager_with_page):
    el = MagicMock()
    el.count = AsyncMock(return_value=1)
    el.first.text_content = AsyncMock(return_value="Максим")
    manager_with_page.page.locator = MagicMock(return_value=el)

    assert await manager_with_page._get_first_name() == "Максим"


# ---------------------------------------------------------------------------
# _set_keywords / _set_search_field (новый интерфейс расширенного поиска)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_keywords_uses_new_search_input_first(manager_with_page):
    """Ключевые слова вводятся в новый инпут search-input."""
    page = manager_with_page.page
    search_input = _make_locator(count=1)
    _route_locators(page, {"search-input": search_input})

    fills = []

    async def fake_fill(_page, selector, text, **_kwargs):
        fills.append(selector)
        return True

    with (
        patch("src.job_manager.playwright_manager.safe_fill", side_effect=fake_fill),
        patch.object(
            manager_with_page,
            "_click_best_suggestion",
            new=AsyncMock(return_value=False),
        ),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
    ):
        manager_with_page.search_params = {"keywords": "Android разработчик"}
        await manager_with_page._set_keywords()

    assert fills[0] == "[data-qa='search-input']"


@pytest.mark.asyncio
async def test_set_search_field_selects_short_description_radio(manager_with_page):
    """name=true в новом интерфейсе -> выбирается радио short_description."""
    page = manager_with_page.page
    radio = _make_locator(count=1)
    _route_locators(page, {"short_description": radio})

    clicks = []

    async def fake_click(_page, selector, **_kwargs):
        clicks.append(selector)
        return True

    with (
        patch("src.job_manager.playwright_manager.safe_click", side_effect=fake_click),
        patch.object(manager_with_page, "pause_async", new_callable=AsyncMock),
    ):
        manager_with_page.search_params = {"search_field": {"name": True}}
        await manager_with_page._set_search_field()

    assert any("short_description" in s for s in clicks)
    assert not any("full_description" in s for s in clicks)


# ---------------------------------------------------------------------------
# _build_search_url
# ---------------------------------------------------------------------------


def test_build_search_url_empty_params(manager):
    url = manager._build_search_url({})
    assert url == "https://hh.ru/search/vacancy?"


def test_build_search_url_keywords(manager):
    url = manager._build_search_url({"keywords": "android"})
    assert "text=android" in url


def test_build_search_url_excluded_words(manager):
    url = manager._build_search_url({"keywords": "dev", "words_to_exclude": "intern,junior"})
    assert "text=dev+-intern+-junior" in url


def test_build_search_url_remote(manager):
    url = manager._build_search_url({"job_format": {"REMOTE": True, "ON_SITE": False, "HYBRID": False, "FIELD_WORK": False}})
    assert "work_format=REMOTE" in url


def test_build_search_url_hybrid_and_remote(manager):
    url = manager._build_search_url({"job_format": {"REMOTE": True, "HYBRID": True, "ON_SITE": False, "FIELD_WORK": False}})
    assert "work_format=REMOTE" in url
    assert "work_format=HYBRID" in url


def test_build_search_url_experience(manager):
    url = manager._build_search_url({"experience": {"doesntMatter": True}})
    assert "experience=doesNotMatter" in url


def test_build_search_url_experience_between(manager):
    url = manager._build_search_url({"experience": {"between1And3": True}})
    assert "experience=between1And3" in url


def test_build_search_url_employment_full(manager):
    url = manager._build_search_url({"employment": {"FULL": True}})
    assert "employment_form=FULL" in url


def test_build_search_url_employment_internship(manager):
    url = manager._build_search_url({"employment": {"INTERNSHIP": True}})
    assert "label=internship" in url


def test_build_search_url_salary_and_currency(manager):
    url = manager._build_search_url({"salary": 150000, "currency": {"RUR": True}})
    assert "salary=150000" in url
    assert "currency_code=RUR" in url


def test_build_search_url_only_with_salary(manager):
    url = manager._build_search_url({"only_with_salary": True})
    assert "label=with_salary" in url


def test_build_search_url_education_higher(manager):
    url = manager._build_search_url({"education": {"higher": True}})
    assert "education=higher" in url


def test_build_search_url_order_by_salary(manager):
    url = manager._build_search_url({"order_by": {"salary_desc": True}})
    assert "order_by=salary_desc" in url


def test_build_search_url_order_by_relevance_omitted(manager):
    url = manager._build_search_url({"order_by": {"relevance": True}})
    assert "order_by" not in url


def test_build_search_url_period_month(manager):
    url = manager._build_search_url({"period": {"month": True}})
    assert "search_period=30" in url


def test_build_search_url_period_all_time_omitted(manager):
    url = manager._build_search_url({"period": {"all_time": True}})
    assert "search_period" not in url


def test_build_search_url_show_50(manager):
    url = manager._build_search_url({"show": {"show_50": True}})
    assert "items_on_page=50" in url


def test_build_search_url_show_20_omitted(manager):
    url = manager._build_search_url({"show": {"show_20": True}})
    assert "items_on_page" not in url


def test_build_search_url_area_numeric(manager):
    url = manager._build_search_url({"area": "113"})
    assert "area=113" in url


def test_build_search_url_area_text_ignored(manager):
    url = manager._build_search_url({"area": "Россия"})
    assert "area" not in url


def test_build_search_url_vacancy_labels(manager):
    url = manager._build_search_url({
        "vacancy_label": {
            "not_from_agency": True,
            "accredited_it": True,
            "with_address": False,
            "accept_handicapped": False,
            "accept_kids": False,
            "accept_teens": False,
            "low_performance": False,
        }
    })
    assert "label=not_from_agency" in url
    assert "label=accredited_it" in url


def test_build_search_url_search_field_description(manager):
    url = manager._build_search_url({"search_field": {"description": True}})
    assert "search_field=description" in url


def test_build_search_url_search_field_name(manager):
    url = manager._build_search_url({"search_field": {"name": True}})
    assert "search_field=name" in url


def test_build_search_url_full_config(manager):
    """Полный набор параметров как в реальном search_config.yaml."""
    url = manager._build_search_url({
        "keywords": "android",
        "experience": {"doesntMatter": True},
        "employment": {"FULL": True},
        "job_format": {"REMOTE": True, "ON_SITE": False, "HYBRID": False, "FIELD_WORK": False},
        "salary": 150000,
        "currency": {"RUR": True},
        "only_with_salary": False,
        "education": {"higher": False, "not_needed": True},
        "order_by": {"relevance": True},
        "period": {"month": True},
        "show": {"show_20": True},
        "vacancy_label": {"not_from_agency": True, "with_address": False, "accept_handicapped": False, "accept_kids": False, "accept_teens": False, "accredited_it": False, "low_performance": False},
    })
    assert "text=android" in url
    assert "experience=doesNotMatter" in url
    assert "employment_form=FULL" in url
    assert "work_format=REMOTE" in url
    assert "salary=150000" in url
    assert "currency_code=RUR" in url
    assert "education=not_required_or_not_specified" in url
    assert "search_period=30" in url
    assert "label=not_from_agency" in url
    assert url.startswith("https://hh.ru/search/vacancy?")
