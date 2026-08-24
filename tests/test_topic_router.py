import pytest

from src.telegram.telegram_bot import TopicRouter

CHAT_ID = "@xx_feedback"


@pytest.fixture
def secrets_full():
    return {
        "tg_chat_id": CHAT_ID,
        "tg_control_topic_id": 346,
        "tg_report_topic_id": 344,
        "tg_jobs_topic_id": 345,
        "tg_err_topic_id": 5,
    }


class TestTopicRouter:
    def test_control_uses_control_topic_when_set(self, secrets_full):
        router = TopicRouter(secrets_full)
        assert router.control() == (CHAT_ID, 346)

    def test_control_falls_back_to_report_topic(self, secrets_full):
        del secrets_full["tg_control_topic_id"]
        router = TopicRouter(secrets_full)
        assert router.control() == (CHAT_ID, 344)

    def test_control_falls_back_to_no_thread(self, secrets_full):
        del secrets_full["tg_control_topic_id"]
        del secrets_full["tg_report_topic_id"]
        router = TopicRouter(secrets_full)
        assert router.control() == (CHAT_ID, None)

    def test_jobs_routing(self, secrets_full):
        router = TopicRouter(secrets_full)
        assert router.jobs() == (CHAT_ID, 345)

    def test_jobs_missing_returns_none(self, secrets_full):
        del secrets_full["tg_jobs_topic_id"]
        router = TopicRouter(secrets_full)
        assert router.jobs() is None

    def test_errors_and_report_routing(self, secrets_full):
        router = TopicRouter(secrets_full)
        assert router.errors() == (CHAT_ID, 5)
        assert router.report() == (CHAT_ID, 344)

    def test_backward_compatible_without_new_keys(self):
        """Старые секреты без control-топика работают"""
        router = TopicRouter(
            {"tg_chat_id": CHAT_ID, "tg_err_topic_id": 5, "tg_report_topic_id": 344}
        )
        assert router.control() == (CHAT_ID, 344)
