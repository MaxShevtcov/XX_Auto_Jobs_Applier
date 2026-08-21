"""Дамп структуры страницы расширенного поиска hh.ru."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.constants import SECRETS_FILE  # noqa: E402
from src.job_manager.playwright_manager import PlaywrightJobManager  # noqa: E402
from src.logger_config import logger  # noqa: E402
from src.utils.utils import load_yaml_file  # noqa: E402

OUT = os.path.join("logs", "debug_response_modal", "advanced_search")


async def main() -> None:
    secrets = load_yaml_file(SECRETS_FILE)
    manager = PlaywrightJobManager(secrets)
    await manager.initialize()
    page = manager.page
    try:
        if not await manager.ensure_logged_in():
            logger.error("Не авторизованы")
            return
        await page.goto(
            "https://hh.ru/search/vacancy/advanced",
            timeout=60000,
            wait_until="domcontentloaded",
        )
        await page.wait_for_timeout(4000)
        os.makedirs(OUT, exist_ok=True)
        with open(os.path.join(OUT, "page.html"), "w", encoding="utf-8") as f:
            f.write(await page.content())

        inputs = await page.evaluate(
            """() => Array.from(document.querySelectorAll('input, textarea')).map(e => ({
                tag: e.tagName,
                type: e.type || '',
                name: e.name || '',
                qa: e.getAttribute('data-qa') || '',
                placeholder: e.placeholder || '',
                aria: e.getAttribute('aria-label') || '',
                cls: (e.className || '').slice(0, 60),
            }))"""
        )
        for i in inputs:
            logger.info(f"INPUT {i}")

        qas = await page.evaluate(
            "() => Array.from(document.querySelectorAll('[data-qa]'))"
            ".map(e => e.getAttribute('data-qa'))"
        )
        interesting = sorted({q for q in qas if q and any(k in q.lower() for k in (
            "keyword", "text", "search", "vacancysearch", "advanced"))})
        logger.info(f"QA-атрибуты ({len(interesting)}): {interesting}")
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
