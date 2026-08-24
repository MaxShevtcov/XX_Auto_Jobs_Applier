from src.utils.utils import load_yaml_file


def build_ptb_request(secrets: dict):
    """
    Собрать HTTPXRequest для python-telegram-bot с учётом прокси:
    tg_proxy из секретов, иначе первый прокси из llm_proxy.
    """
    from telegram.request import HTTPXRequest

    proxy_url = secrets.get("tg_proxy")
    if not proxy_url:
        llm_proxy = secrets.get("llm_proxy")
        if isinstance(llm_proxy, list) and llm_proxy:
            proxy_url = llm_proxy[0]
        elif isinstance(llm_proxy, str) and llm_proxy:
            proxy_url = llm_proxy

    return HTTPXRequest(
        connection_pool_size=10,
        pool_timeout=20,
        read_timeout=20,
        write_timeout=20,
        connect_timeout=10,
        proxy=proxy_url,
    )


def load_raw_secrets(secrets_file: str) -> dict:
    """Загрузить секреты без pydantic-валидации (все ключи сохраняются)"""
    return load_yaml_file(secrets_file)
