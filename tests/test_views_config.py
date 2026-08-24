import yaml

VALID_SECRETS = {
    "hh_login": "test_login",
    "hh_password": "test_password",
    "llm_api_key": "test_key",
    "llm_proxy": ["proxy1", "proxy2"],
    "tg_token": "test_token",
    "tg_api_id": "test_id",
    "tg_api_hash": "test_hash",
}


class TestSecretsNewFields:
    def test_old_secrets_still_valid(self):
        from src.views.config import Secrets

        secrets = Secrets(**VALID_SECRETS)
        assert secrets.tg_control_topic_id is None
        assert secrets.tg_allowed_user_ids == []

    def test_control_topic_optional_int(self):
        from src.views.config import Secrets

        data = dict(VALID_SECRETS, tg_control_topic_id=346)
        secrets = Secrets(**data)
        assert secrets.tg_control_topic_id == 346

    def test_control_topic_accepts_string_digits(self):
        from src.views.config import Secrets

        data = dict(VALID_SECRETS, tg_control_topic_id="346")
        secrets = Secrets(**data)
        assert secrets.tg_control_topic_id == 346

    def test_allowed_user_ids_from_comma_string(self):
        from src.views.config import Secrets

        data = dict(VALID_SECRETS, tg_allowed_user_ids="123,456")
        secrets = Secrets(**data)
        assert secrets.tg_allowed_user_ids == [123, 456]

    def test_allowed_user_ids_none_becomes_empty_list(self):
        from src.views.config import Secrets

        data = dict(VALID_SECRETS, tg_allowed_user_ids=None)
        secrets = Secrets(**data)
        assert secrets.tg_allowed_user_ids == []

    def test_extra_keys_ignored(self):
        """Старые конфиги с tg_chat_id/топиками валидируются без правок"""
        from src.views.config import Secrets

        data = dict(VALID_SECRETS, tg_chat_id="@xx_feedback", tg_err_topic_id=5)
        secrets = Secrets(**data)
        assert secrets.tg_token == "test_token"

    def test_full_secrets_file_roundtrip(self, tmp_path):
        from src.views.config import Secrets

        path = tmp_path / "secrets.yaml"
        path.write_text(
            yaml.safe_dump(
                dict(
                    VALID_SECRETS,
                    tg_chat_id="@xx_feedback",
                    tg_report_topic_id=344,
                    tg_jobs_topic_id=345,
                    tg_err_topic_id=5,
                    tg_captcha_topic_id=17,
                    tg_control_topic_id=346,
                    tg_allowed_user_ids="111, 222",
                )
            ),
            encoding="utf-8",
        )
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        secrets = Secrets(**raw)
        assert secrets.tg_allowed_user_ids == [111, 222]
