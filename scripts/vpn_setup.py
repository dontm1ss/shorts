"""Настройка WireGuard-сервера на арендованном VPS и генерация клиентского конфига для телефона.

Подключение — paramiko (пароль или ключ). Реквизиты сервера читаются из .env (не в git):
    VPN_SERVER_HOST, VPN_SERVER_USER, VPN_SERVER_PASSWORD
Клиентский конфиг сохраняется в output/phone/wg0.conf (папка output — не в git).

    venv\\Scripts\\python scripts/vpn_setup.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import paramiko

from core.config import ROOT, read_env_file

SCRIPT = ROOT / "scripts" / "vpn_server_setup.sh"


def _creds() -> tuple[str, str, str]:
    env = read_env_file()
    value = lambda name: (env.get(name) or "").strip() or None  # noqa: E731
    host, user, password = value("VPN_SERVER_HOST"), value("VPN_SERVER_USER"), value("VPN_SERVER_PASSWORD")
    if not (host and user and password):
        raise SystemExit("В .env нет VPN_SERVER_HOST / VPN_SERVER_USER / VPN_SERVER_PASSWORD")
    return host, user, password


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    host, user, password = _creds()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=30, look_for_keys=False, allow_agent=False)

    # не дублировать пароль в команду: скрипт загружается по stdin
    stdin, stdout, stderr = client.exec_command("bash -s -- " + host, timeout=600)
    stdin.write(SCRIPT.read_text(encoding="utf-8"))
    stdin.channel.shutdown_write()
    out, err = stdout.read().decode("utf-8", "replace"), stderr.read().decode("utf-8", "replace")
    code = stdout.channel.recv_exit_status()
    client.close()

    print(out)
    if code != 0:
        print(f"Сервер вернул ошибку (код {code}):\n{err}", file=sys.stderr)
        return 1

    # клиентский конфиг — всё, что после последней строки установки, начинается с [Interface]
    idx = out.rfind("[Interface]")
    if idx < 0:
        print("Не удалось вычленить клиентский конфиг из вывода", file=sys.stderr)
        return 1
    conf = out[idx:].strip() + "\n"
    dest = ROOT / "output" / "phone" / "wg0.conf"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(conf, encoding="utf-8")
    print(f"\nКлиентский конфиг сохранён: {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
