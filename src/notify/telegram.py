"""Сообщения в Telegram. Токен бота и чат — в .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; чат находит
`python -m notify setup`). Читаются при каждой отправке: добавленный токен работает без перезапуска
панели и обработчика. Ошибка отправки только пишется в лог, без токена, — конвейер из-за Telegram не падает.
"""
from __future__ import annotations

import logging
import os

import requests

from core import config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def credentials() -> tuple[str | None, str | None]:
    env = config.read_env_file()
    value = lambda name: os.environ.get(name) or env.get(name) or None  # noqa: E731
    return value("TELEGRAM_BOT_TOKEN"), value("TELEGRAM_CHAT_ID")


def call(token: str, method: str, timeout: float = 15, **params) -> dict | list:
    """Метод Bot API → result. В тексте ошибки токена нет: у requests он был бы в адресе."""
    try:
        resp = requests.post(API.format(token=token, method=method), json=params, timeout=timeout)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise TelegramError(str(e).replace(token, "***")) from None
    if not data.get("ok"):
        raise TelegramError(f"{resp.status_code}: {data.get('description', '')}")
    return data["result"]


def notify(cfg: dict, text: str) -> bool:
    """Сообщение владельцу. False — уведомления выключены, не настроены или Telegram не ответил."""
    if not cfg["notify"]["enabled"]:
        return False
    token, chat = credentials()
    if not token or not chat:
        return False
    try:
        call(token, "sendMessage", chat_id=chat, text=text[:4000], disable_web_page_preview=True)
    except TelegramError as e:
        log.warning("Telegram: сообщение не отправлено — %s", e)
        return False
    return True
