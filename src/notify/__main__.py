"""Подключение уведомлений в Telegram (проще — кнопкой «Подключить Telegram» в панели).

    venv\\Scripts\\python -m notify setup            # после /start боту: найти чат и записать TELEGRAM_CHAT_ID в .env
    venv\\Scripts\\python -m notify setup --chat ID  # если боту писали несколько человек
    venv\\Scripts\\python -m notify test ["текст"]   # проверочное сообщение

Токен бота (TELEGRAM_BOT_TOKEN) вписывается в .env вручную — от @BotFather.
"""
import argparse
import sys

from core import config
from notify.telegram import TelegramError, call, credentials, notify, save_env


def private_chats(updates: list[dict]) -> list[dict]:
    """Личные чаты, писавшие боту, — последний написавший первым."""
    chats = {}
    for update in updates:
        chat = (update.get("message") or {}).get("chat") or {}
        if chat.get("type") == "private":
            chats.pop(chat["id"], None)
            chats[chat["id"]] = chat
    return list(reversed(chats.values()))


def _who(chat: dict) -> str:
    name = " ".join(filter(None, (chat.get("first_name"), chat.get("last_name"))))
    return f"{chat['id']} — {name}" + (f" (@{chat['username']})" if chat.get("username") else "")


def cmd_setup(args, cfg) -> int:
    token, _ = credentials()
    if not token:
        print("В .env нет TELEGRAM_BOT_TOKEN — добавьте строку TELEGRAM_BOT_TOKEN=123456789:ABC… (токен от @BotFather)")
        return 1
    try:
        bot = call(token, "getMe")
        chats = private_chats(call(token, "getUpdates", http_timeout=30))
    except TelegramError as e:
        print(f"Telegram не ответил: {e}")
        return 1
    print(f"Бот @{bot['username']}")
    if args.chat:
        chat = args.chat
    elif len(chats) == 1:
        chat = str(chats[0]["id"])
        print(f"Чат: {_who(chats[0])}")
    elif not chats:
        print(f"Напишите боту @{bot['username']} в Telegram /start и запустите setup ещё раз")
        return 1
    else:
        print("Боту писали несколько человек — выберите свой чат: python -m notify setup --chat ID")
        for c in chats:
            print("  " + _who(c))
        return 1
    save_env("TELEGRAM_CHAT_ID", chat)
    sent = notify(cfg, "✅ Уведомления фермы shorts подключены: упавшие задачи, готовые ролики, "
                       "сценарии на проверке и перезапуск обработчика")
    print("TELEGRAM_CHAT_ID записан в .env" + ("; проверочное сообщение отправлено" if sent
                                              else "; сообщение отправить не удалось — см. лог выше"))
    return 0 if sent else 1


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m notify")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("setup", help="найти чат владельца и записать его в .env")
    p.add_argument("--chat", help="id чата, если боту писали несколько человек")
    p = sub.add_parser("test", help="проверочное сообщение")
    p.add_argument("text", nargs="?", default="🔔 Проверка уведомлений фермы shorts")
    args = parser.parse_args(argv)
    cfg = config.load_config()
    if args.cmd == "setup":
        return cmd_setup(args, cfg)
    if not all(credentials()):
        print("Уведомления не настроены: нужны TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID в .env (python -m notify setup)")
        return 1
    ok = notify(cfg, args.text)
    print("Отправлено" if ok else "Не отправлено — см. лог выше")
    return 0 if ok else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
