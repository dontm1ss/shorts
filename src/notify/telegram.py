"""Сообщения в Telegram. Токен бота и чат — в .env (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID; чат подключается
кнопкой в панели или `python -m notify setup`). Читаются при каждой отправке: добавленный токен работает
без перезапуска панели и обработчика. Ошибка отправки только пишется в лог, без токена, — конвейер
из-за Telegram не падает.
"""
from __future__ import annotations

import json
import logging
import os

import requests

from core import config

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


class TelegramError(RuntimeError):
    pass


def credentials() -> tuple[str | None, str | None]:
    """Токен бота и чат владельца. Скобки вокруг значения («<токен>», как в шаблоне инструкции) отбрасываются."""
    env = config.read_env_file()
    value = lambda name: (os.environ.get(name) or env.get(name) or "").strip().strip("<>").strip() or None  # noqa: E731
    return value("TELEGRAM_BOT_TOKEN"), value("TELEGRAM_CHAT_ID")


def save_env(key: str, value: str) -> None:
    """Задать ключ в .env: прежняя строка с ним заменяется, остальные остаются как были."""
    path = config.ENV_FILE
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if line.split("=", 1)[0].strip() != key]
    path.write_text("\n".join([*lines, f"{key}={value}"]) + "\n", encoding="utf-8")


def call(token: str, method: str, http_timeout: float = 15, files: dict | None = None, **params) -> dict | list:
    """Метод Bot API → result. files — загрузка файла (видео): тогда запрос multipart, а вложенные
    параметры (reply_markup) — JSON-строкой. В тексте ошибки токена нет: у requests он был бы в адресе."""
    url = API.format(token=token, method=method)
    try:
        if files:
            form = {k: v if isinstance(v, str) else json.dumps(v, ensure_ascii=False) for k, v in params.items()}
            resp = requests.post(url, data=form, files=files, timeout=http_timeout)
        else:
            resp = requests.post(url, json=params, timeout=http_timeout)
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
