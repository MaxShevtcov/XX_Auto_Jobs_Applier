---
name: security-review
description: Security review for XX_Auto_Jobs_Applier — credentials in data_folder/secrets, hh.ru account safety, personal data anonymization before LLM, Telegram tokens. Use when reviewing changes touching credentials, personal data, LLM calls or browser sessions.
---

## High-risk areas

1. **secrets.yaml** — `data_folder/secrets/secrets.yaml` содержит hh_login/hh_password, llm_api_key, llm_proxy (логин:пароль@IP:порт), tg_token, tg_api_id/tg_api_hash. Проверить: файл не коммитится, значения не попадают в логи, исключения, промпты и отчёты Telegram
2. **Анонимизация перед LLM** — все персональные данные (кроме города и даты рождения) заменяются на `DUMMY_PERSONAL_INFO_MALE/FEMALE` из `src/constants.py`. Новые персональные поля обязаны проходить ту же замену до отправки в `LlmManager`
3. **Логи LLM API** — логи вызовов и `parse_llm_api_calls.py` содержат тексты вакансий/резюме; убедиться что секреты и персональные данные не просачиваются туда в сыром виде
4. **Прокси** — `llm_proxy` не должен логироваться целиком (там пароль); маскировать креды при выводе
5. **chrome_profile/** — куки сессии hh.ru. Не коммитить, не копировать, не отправлять; `.gitignore` должен покрывать
6. **Telegram токены** — tg_token/tg_api_id/tg_api_hash только из secrets.yaml; ID чатов/топиков в `src/llm/constants.py` — не хардкодить новые значения в коде
7. **Безопасность аккаунта hh.ru** — соблюдение MINIMUM_WAIT_TIME_SEC, apply_once_at_company, лимитов откликов: агрессивная автоматизация ведёт к блокировке аккаунта пользователя
8. **YAML загрузка** — только `yaml.safe_load`, никогда `yaml.load` (произвольное исполнение кода)
9. **Капча через telethon** — решение капчи приходит сообщением из чата; валидировать содержимое перед вводом на страницу
