from datetime import datetime
from pathlib import Path
from typing import Optional, Union
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger
from pydantic import BaseModel, field_validator

from src.constants import DEFAULT_SCHEDULE_CRON, DEFAULT_SCHEDULE_TZ, SCHEDULE_FILE
from src.logger_config import logger

DEFAULT_GRACE_SEC = 3600


class ScheduleConfig(BaseModel):
    """Расписание ежедневного автопоиска (персистится в schedule.yaml)"""

    enabled: bool = True
    cron: str = DEFAULT_SCHEDULE_CRON
    timezone: str = DEFAULT_SCHEDULE_TZ
    misfire_grace_time_sec: int = DEFAULT_GRACE_SEC

    @field_validator("cron")
    @classmethod
    def validate_cron(cls, v: str) -> str:
        CronTrigger.from_crontab(v)
        return v

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, v: str) -> str:
        ZoneInfo(v)
        return v


class ScheduleStore:
    """Чтение/запись расписания из data_folder/schedule/schedule.yaml"""

    def __init__(self, path: Union[str, Path] = SCHEDULE_FILE):
        self.path = Path(path)

    def load(self) -> ScheduleConfig:
        """Прочитать расписание; битый/отсутствующий файл → дефолт + warning"""
        try:
            import yaml

            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            return ScheduleConfig(**data)
        except FileNotFoundError:
            logger.warning(f"Файл расписания не найден: {self.path}, используем дефолт")
        except Exception as e:
            logger.warning(f"Битый файл расписания {self.path} ({e}), используем дефолт")
        return ScheduleConfig()

    def save(self, cfg: ScheduleConfig, path: Optional[Path] = None) -> None:
        """Атомарная запись расписания (tmp + replace), папка создаётся при необходимости"""
        import os
        import tempfile

        import yaml

        target = Path(path) if path else self.path
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(cfg.model_dump(), f, allow_unicode=True, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    def validate_user_input(
        self, text: str, enabled: bool = True
    ) -> tuple[bool, Union[ScheduleConfig, str]]:
        """
        Разобрать ввод пользователя из Telegram.

        Поддерживаются форматы:
          "<cron>"                    — "0 9 * * *"
          "<cron> <TZ>"               — "30 8 * * * Europe/Moscow"
          "HH:MM"                     — "09:30" (= "M H * * *")

        Возвращает (True, ScheduleConfig) либо (False, сообщение об ошибке).
        """
        text = (text or "").strip()
        if not text:
            return False, "Пустой ввод. Примеры: `0 9 * * *`, `0 9 * * * Europe/Moscow`, `09:30`"

        parts = text.split()
        # Формат HH:MM → cron "M H * * *" в дефолтной таймзоне
        if len(parts) == 1 and ":" in parts[0]:
            hhmm = parts[0].split(":")
            if (
                len(hhmm) == 2
                and hhmm[0].isdigit()
                and hhmm[1].isdigit()
                and 0 <= int(hhmm[0]) <= 23
                and 0 <= int(hhmm[1]) <= 59
            ):
                try:
                    return True, ScheduleConfig(
                        cron=f"{int(hhmm[1])} {int(hhmm[0])} * * *",
                        timezone=DEFAULT_SCHEDULE_TZ,
                        enabled=enabled,
                    )
                except Exception as e:
                    return False, f"Некорректное время: {e}"
            return False, "Некорректное время. Пример: `09:30`"

        tz_part = DEFAULT_SCHEDULE_TZ
        cron_part = text
        if len(parts) >= 6:
            cron_part = " ".join(parts[:5])
            tz_part = parts[-1]

        try:
            ZoneInfo(tz_part)
        except Exception:
            return False, (
                f"Неизвестный часовой пояс `{tz_part}`. " f"Пример IANA-имени: Europe/Kaliningrad"
            )
        try:
            cfg = ScheduleConfig(cron=cron_part, timezone=tz_part, enabled=enabled)
        except Exception as e:
            return False, (
                f"Некорректное cron-выражение: {e}. "
                f"Примеры: `0 9 * * *`, `30 8 * * 1-5`, или просто `09:30`"
            )
        return True, cfg


def describe_next_runs(cfg: ScheduleConfig, count: int = 3, now=None) -> list[datetime]:
    """Следующие N запусков по расписанию (для превью в меню)"""
    trigger = CronTrigger.from_crontab(cfg.cron, timezone=cfg.timezone)
    now = now or datetime.now(trigger.timezone)
    runs = []
    fire_time = None
    for _ in range(count):
        fire_time = trigger.get_next_fire_time(fire_time, now)
        if fire_time is None:
            break
        runs.append(fire_time)
        now = fire_time
    return runs
