"""Read-only search over public Telegram channel web previews.

The module deliberately uses ``t.me/s/<channel>`` pages rather than MTProto:
no Telegram user session, phone number, cookies or API credentials are used.
"""

import asyncio
import html
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from src.application.task_queue import SOURCE_TELEGRAM_SEARCH, Task, TaskQueue
from src.constants import (
    TELEGRAM_SEARCH_STATE_FILE,
    TELEGRAM_SOURCES_EXAMPLE_FILE,
    TELEGRAM_SOURCES_FILE,
)
from src.llm.llm_manager import LLMDailyRateLimitError
from src.logger_config import logger
from src.telegram.telegram_manager import TelegramReportSender
from src.utils.utils import load_app_config
from src.views.job import JobDescription

VACANCY_RE = re.compile(
    r"\b(ваканси[яию]|ищем|hiring|job|работа|developer|engineer|разработчик|"
    r"программист|frontend|backend|fullstack|python|java|golang|react|ai|ml)\b",
    re.IGNORECASE,
)
USERNAME_RE = re.compile(r"(?<![\w@])@([a-zA-Z][\w]{4,31})")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d\s().-]{8,}\d)")
URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)
MAX_POST_TEXT = 12000


@dataclass
class PublicPost:
    source: str
    username: str
    message_id: int
    text: str
    published_at: str
    links: list[str]

    @property
    def permalink(self) -> str:
        return f"https://t.me/{self.username}/{self.message_id}"


class _PreviewParser(HTMLParser):
    """Small dependency-free parser for Telegram's public preview markup."""

    def __init__(self, source: str, username: str):
        super().__init__(convert_charrefs=True)
        self.source, self.username = source, username
        self.posts: list[PublicPost] = []
        self._current: Optional[dict] = None
        self._div_depth = 0
        self._message_text_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = attrs.get("class", "")
        if tag == "div" and "tgme_widget_message" in classes and attrs.get("data-post"):
            value = attrs["data-post"].rsplit("/", 1)[-1]
            if value.isdigit():
                self._current = {"id": int(value), "text": [], "links": [], "time": ""}
                self._div_depth = 1
                return
        if self._current is not None:
            if tag == "div":
                self._div_depth += 1
                if "tgme_widget_message_text" in classes:
                    self._message_text_depth = 1
                elif self._message_text_depth:
                    self._message_text_depth += 1
            if tag == "a" and self._message_text_depth and attrs.get("href"):
                self._current["links"].append(html.unescape(attrs["href"]))
            if tag == "time" and attrs.get("datetime"):
                self._current["time"] = attrs["datetime"]

    def handle_endtag(self, tag):
        if self._current is None:
            return
        if tag == "div" and self._message_text_depth:
            self._message_text_depth -= 1
        if tag != "div":
            return
        self._div_depth -= 1
        if self._div_depth > 0:
            return
        text = " ".join("".join(self._current["text"]).split())
        self.posts.append(
            PublicPost(
                source=self.source,
                username=self.username,
                message_id=self._current["id"],
                text=text,
                published_at=self._current["time"],
                links=list(dict.fromkeys(self._current["links"])),
            )
        )
        self._current = None
        self._message_text_depth = 0

    def handle_data(self, data):
        if self._current is not None and self._message_text_depth:
            self._current["text"].append(data)


class TelegramJobSearchRunner:
    """Incrementally find suitable vacancies in an explicit public-channel allowlist."""

    def __init__(
        self,
        parameters: dict,
        cover_letter_service,
        sources_path: str = TELEGRAM_SOURCES_FILE,
        example_sources_path: str = TELEGRAM_SOURCES_EXAMPLE_FILE,
        state_path: str = TELEGRAM_SEARCH_STATE_FILE,
    ):
        self.parameters = parameters
        self.cover_letter_service = cover_letter_service
        self.sources_path = Path(sources_path)
        self.example_sources_path = Path(example_sources_path)
        self.state_path = Path(state_path)

    def config(self) -> dict:
        path = self.sources_path if self.sources_path.exists() else self.example_sources_path
        if not path.exists():
            return {"enabled": False, "sources": []}
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as error:
            raise RuntimeError(f"Некорректный sources.yaml: {error}") from error
        settings = raw.get("telegram_search") or {}
        app_config = load_app_config()
        return {
            "enabled": bool(settings.get("enabled", True)),
            "interval_minutes": max(15, int(settings.get("interval_minutes", 120))),
            "initial_lookback_days": max(1, int(settings.get("initial_lookback_days", 3))),
            "max_results_per_run": max(1, int(settings.get("max_results_per_run", 20))),
            "max_pages_per_source": max(1, int(settings.get("max_pages_per_source", 5))),
            "request_delay_seconds": max(1, float(settings.get("request_delay_seconds", 2))),
            "llm_timeout_seconds": max(5, min(120, int(settings.get("llm_timeout_seconds", 45)))),
            "min_score": max(
                0,
                min(100, int(settings.get("min_score", app_config.get("JOB_IS_INTERESTING_THRESH", 70)))),
            ),
            "sources": [item for item in raw.get("sources", []) if item.get("enabled", True)],
        }

    def _load_state(self) -> dict:
        try:
            return yaml.safe_load(self.state_path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            return {"sources": {}, "sent": {}}
        except yaml.YAMLError as error:
            logger.warning(f"Состояние Telegram-поиска повреждено: {error}")
            return {"sources": {}, "sent": {}}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.state_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                yaml.safe_dump(state, stream, allow_unicode=True, sort_keys=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.state_path)
        except Exception:
            if os.path.exists(temporary):
                os.unlink(temporary)
            raise

    async def _fetch_source(
        self, client: httpx.AsyncClient, source: dict, state: dict, cfg: dict
    ) -> list[PublicPost]:
        username = str(source.get("username", "")).lstrip("@").strip()
        if not re.fullmatch(r"[A-Za-z][\w]{4,31}", username):
            logger.warning(f"Пропускаем источник с некорректным username: {username!r}")
            return []
        source_state = state.setdefault("sources", {}).setdefault(username, {})
        watermark = int(source_state.get("last_message_id", 0) or 0)
        cutoff = datetime.now(timezone.utc) - timedelta(days=cfg["initial_lookback_days"])
        posts: list[PublicPost] = []
        before: Optional[int] = None
        for page_num in range(cfg["max_pages_per_source"]):
            url = f"https://t.me/s/{username}" + (f"?before={before}" if before else "")
            response = await client.get(url)
            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "300"))
                raise RuntimeError(
                    f"Telegram ограничил запросы для {username}; повтор через {retry_after} с"
                )
            response.raise_for_status()
            parser = _PreviewParser(str(source.get("name", username)), username)
            parser.feed(response.text)
            page = parser.posts
            if not page:
                break
            posts.extend(
                post
                for post in page
                if post.message_id > watermark and not self._post_before(post, cutoff)
            )
            oldest = min(post.message_id for post in page)
            old_enough = any(self._post_before(post, cutoff) for post in page)
            if oldest <= watermark or old_enough:
                break
            before = oldest
            if page_num + 1 < cfg["max_pages_per_source"]:
                await asyncio.sleep(cfg["request_delay_seconds"])
        return sorted({post.message_id: post for post in posts}.values(), key=lambda item: item.message_id)

    @staticmethod
    def _post_before(post: PublicPost, cutoff: datetime) -> bool:
        try:
            return datetime.fromisoformat(post.published_at.replace("Z", "+00:00")) < cutoff
        except (AttributeError, ValueError):
            return False

    @staticmethod
    def _contacts(post: PublicPost) -> tuple[list[str], list[str]]:
        text = post.text
        contacts = [f"@{value}" for value in USERNAME_RE.findall(text)]
        contacts.extend(EMAIL_RE.findall(text))
        contacts.extend(match.strip() for match in PHONE_RE.findall(text))
        links = list(post.links) + URL_RE.findall(text)
        application_links = []
        for link in links:
            parsed = urlparse(link)
            if not parsed.scheme:
                continue
            if parsed.netloc in {"t.me", "telegram.me"} and parsed.path.strip("/"):
                handle = parsed.path.strip("/").split("/")[0]
                if handle and handle != post.username:
                    contacts.append(f"@{handle}")
            else:
                application_links.append(link)
        return list(dict.fromkeys(contacts)), list(dict.fromkeys(application_links))

    @staticmethod
    def _looks_like_vacancy(post: PublicPost) -> bool:
        return bool(post.text and VACANCY_RE.search(post.text))

    @staticmethod
    def _masked_text(text: str, contacts: list[str]) -> str:
        result = text[:MAX_POST_TEXT]
        for number, contact in enumerate(sorted(contacts, key=len, reverse=True), start=1):
            result = result.replace(contact, f"CONTACT_{number}")
        return result

    @staticmethod
    def _job_from_post(post: PublicPost, text: str) -> dict:
        lines = [line.strip(" -–—#") for line in post.text.splitlines() if line.strip()]
        title = next((line for line in lines if len(line) < 160), "Вакансия из Telegram")
        salary_match = re.search(r"(?:от\s*)?[\d\s]{2,}(?:₽|руб\.?|\$|€|USD|EUR)", post.text, re.I)
        location_match = re.search(r"(?:локация|город)\s*[:—-]\s*([^\n]{2,80})", post.text, re.I)
        company_match = re.search(
            r"(?:компания|работодатель)\s*[:—-]\s*([^\n]{2,100})", post.text, re.I
        )
        return {
            "job_title": title,
            "company_name": company_match.group(1).strip() if company_match else "Не указана",
            "vacancy_id": f"tg:{post.username}:{post.message_id}",
            "description": text,
            "salary": salary_match.group(0).strip() if salary_match else "",
            "work_formats": "",
            "experience": "",
            "employment": "",
            "skills": "",
            "location": location_match.group(1).strip() if location_match else "",
        }

    async def run_search(self) -> dict:
        cfg = self.config()
        if not cfg["enabled"]:
            return {"sent": 0, "checked": 0, "reason": "Telegram-поиск отключён"}
        state = self._load_state()
        state.setdefault("sources", {})
        state.setdefault("sent", {})
        checked = sent = 0
        daily_limit_reached = False
        sender = TelegramReportSender()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "XX-Auto-Jobs-Applier/1.0 (public job search)"},
            follow_redirects=True,
        ) as client:
            for source_index, source in enumerate(cfg["sources"]):
                if sent >= cfg["max_results_per_run"]:
                    break
                username = str(source.get("username", "")).lstrip("@")
                try:
                    posts = await self._fetch_source(client, source, state, cfg)
                except Exception as error:
                    logger.warning(f"Не удалось прочитать канал {username}: {error}")
                    continue
                processed_last_id = int(
                    state.setdefault("sources", {}).setdefault(username, {}).get("last_message_id", 0)
                    or 0
                )
                for post in posts:
                    checked += 1
                    key = f"{post.username}:{post.message_id}"
                    if key in state["sent"] or not self._looks_like_vacancy(post):
                        processed_last_id = post.message_id
                        continue
                    contacts, application_links = self._contacts(post)
                    if not contacts and not application_links:
                        processed_last_id = post.message_id
                        continue
                    job = self._job_from_post(post, self._masked_text(post.text, contacts))
                    processed_last_id = post.message_id
                    try:
                        score_data, letter = await asyncio.wait_for(
                            self.cover_letter_service.score_and_generate_job(
                                job, self.parameters, threshold=cfg["min_score"]
                            ),
                            timeout=cfg["llm_timeout_seconds"],
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"Пропускаем {post.permalink}: ИИ не ответил за "
                            f"{cfg['llm_timeout_seconds']} с"
                        )
                        continue
                    except LLMDailyRateLimitError as error:
                        logger.warning(f"Останавливаем Telegram-поиск: {error}")
                        daily_limit_reached = True
                        break
                    except Exception as error:
                        logger.warning(f"Пропускаем {post.permalink}: ошибка ИИ: {error}")
                        continue
                    score = int(score_data.get("score", 0) or 0)
                    if score < cfg["min_score"] or not letter:
                        continue
                    description = JobDescription(
                        job_title=job["job_title"],
                        company_name=job["company_name"],
                        vacancy_id=job["vacancy_id"],
                        link=post.permalink,
                        cover_letter=letter,
                        job_score=score,
                        source=post.source,
                        published_at=post.published_at,
                        salary=job["salary"],
                        location=job["location"],
                        contacts=contacts,
                        application_links=application_links,
                        apply_status="Требуется ручной отклик",
                    )
                    await sender.send_job_description(description)
                    state["sent"][key] = datetime.now(timezone.utc).isoformat()
                    sent += 1
                    if sent >= cfg["max_results_per_run"]:
                        break
                if daily_limit_reached:
                    break
                if processed_last_id:
                    state["sources"].setdefault(username, {})["last_message_id"] = processed_last_id
                if source_index + 1 < len(cfg["sources"]):
                    await asyncio.sleep(cfg["request_delay_seconds"])
        # bounded dedup history; IDs in per-source watermark are the durable cursor.
        if len(state["sent"]) > 2000:
            state["sent"] = dict(list(state["sent"].items())[-1000:])
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        self._save_state(state)
        return {
            "sent": sent,
            "checked": checked,
            "reason": "дневной лимит LLM исчерпан" if daily_limit_reached else "готово",
        }


class TelegramJobScheduler:
    """Low-frequency scheduler for the read-only Telegram search."""

    JOB_ID = "telegram_job_search"

    def __init__(self, task_queue: TaskQueue, runner: TelegramJobSearchRunner):
        self.task_queue, self.runner = task_queue, runner
        self.scheduler: Optional[AsyncIOScheduler] = None

    async def start(self) -> None:
        cfg = self.runner.config()
        if not cfg["enabled"]:
            return
        self.scheduler = AsyncIOScheduler()
        self.scheduler.add_job(
            self._fire,
            IntervalTrigger(minutes=cfg["interval_minutes"]),
            id=self.JOB_ID,
            coalesce=True,
            max_instances=1,
            replace_existing=True,
        )
        self.scheduler.start()
        self._fire()

    async def shutdown(self) -> None:
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
            self.scheduler = None

    def next_fire_time(self):
        job = self.scheduler.get_job(self.JOB_ID) if self.scheduler else None
        return job.next_run_time if job else None

    def _fire(self) -> Optional[int]:
        return self.task_queue.enqueue(
            Task(
                source=SOURCE_TELEGRAM_SEARCH,
                coro_factory=self.runner.run_search,
                dedupe_key="telegram_search",
            )
        )
