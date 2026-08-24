import pytest
import yaml

from src.application.schedule_store import DEFAULT_GRACE_SEC, ScheduleConfig, ScheduleStore


class TestScheduleConfig:
    def test_defaults(self):
        cfg = ScheduleConfig()
        assert cfg.enabled is True
        assert cfg.cron == "0 9 * * *"
        assert cfg.timezone == "Europe/Kaliningrad"
        assert cfg.misfire_grace_time_sec == DEFAULT_GRACE_SEC

    def test_invalid_cron_raises(self):
        with pytest.raises(Exception):
            ScheduleConfig(cron="not a cron")

    def test_invalid_timezone_raises(self):
        with pytest.raises(Exception):
            ScheduleConfig(timezone="Bad/Zone")


class TestScheduleStore:
    def test_load_defaults_when_file_missing(self, tmp_path):
        store = ScheduleStore(tmp_path / "schedule.yaml")
        cfg = store.load()
        assert cfg == ScheduleConfig()

    def test_load_valid_file_roundtrip(self, tmp_path):
        path = tmp_path / "schedule.yaml"
        data = {
            "enabled": False,
            "cron": "30 8 * * *",
            "timezone": "Europe/Moscow",
            "misfire_grace_time_sec": 600,
        }
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        store = ScheduleStore(path)
        cfg = store.load()
        assert cfg.enabled is False
        assert cfg.cron == "30 8 * * *"
        assert cfg.timezone == "Europe/Moscow"
        assert cfg.misfire_grace_time_sec == 600

    def test_invalid_cron_falls_back_to_default(self, tmp_path):
        path = tmp_path / "schedule.yaml"
        path.write_text(yaml.safe_dump({"cron": "garbage"}), encoding="utf-8")
        store = ScheduleStore(path)
        cfg = store.load()
        assert cfg == ScheduleConfig()

    def test_invalid_timezone_falls_back_to_default(self, tmp_path):
        path = tmp_path / "schedule.yaml"
        path.write_text(yaml.safe_dump({"timezone": "Mars/Olympus"}), encoding="utf-8")
        store = ScheduleStore(path)
        cfg = store.load()
        assert cfg.timezone == "Europe/Kaliningrad"

    def test_save_atomic_write(self, tmp_path):
        path = tmp_path / "sub" / "schedule.yaml"
        store = ScheduleStore(path)
        cfg = ScheduleConfig(cron="15 10 * * 1-5", timezone="Europe/Moscow")
        store.save(cfg)

        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert loaded["cron"] == "15 10 * * 1-5"
        assert loaded["timezone"] == "Europe/Moscow"
        # не осталось временных файлов после атомарной записи
        assert list(path.parent.iterdir()) == [path]

    def test_validate_accepts_cron_with_tz(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        ok, result = store.validate_user_input("30 8 * * * Europe/Moscow")
        assert ok is True
        assert result.cron == "30 8 * * *"
        assert result.timezone == "Europe/Moscow"

    def test_validate_accepts_cron_only(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        ok, result = store.validate_user_input("0 9 * * *")
        assert ok is True
        assert result.timezone == "Europe/Kaliningrad"

    def test_validate_accepts_hhmm(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        ok, result = store.validate_user_input("09:30")
        assert ok is True
        assert result.cron == "30 9 * * *"

    def test_validate_rejects_garbage_with_message(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        ok, result = store.validate_user_input("завтра утром")
        assert ok is False
        assert isinstance(result, str)
        assert result

    def test_validate_preserves_enabled(self, tmp_path):
        store = ScheduleStore(tmp_path / "s.yaml")
        ok, result = store.validate_user_input("12 12 * * *", enabled=False)
        assert ok is True
        assert result.enabled is False
