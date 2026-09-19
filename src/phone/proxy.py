"""Прокси/VPN на телефоне: подготовка конфига WireGuard и пошаговые инструкции.

Прокси на устройстве нужен, чтобы выходить в интернет из нужной страны (для ру-сегмента
TikTok — из-за границы, потому что ТикТок РФ с 2022-го не даёт постить из России).

Что важно (подробнее — docs/phone-automation.md):
- НЕ общий датацентр-VPN на все аккаунты: один IP на несколько аккаунтов связывает их,
  и банят пачкой. Для одного телефона (= одного аккаунта) подходит личный VPN/мобильный прокси.
- Лучший по чистоте вариант — мобильный прокси нужной страны (IP настоящего оператора),
  запасной — личный VPN на арендованном VPS (WireGuard/Outline).

На Android без root можно только завернуть весь трафик в VPN (или per-app через v2rayNG
с правилами). Глобальный http-прокси через adb работает только по Wi-Fi и приложения часто
его игнорируют — он здесь как служебный, а не основной.
"""
from __future__ import annotations

from phone import device


def wireguard_conf(interface_privkey: str, server_pubkey: str, server_endpoint: str,
                   client_ip: str, dns: str = "1.1.1.1, 1.0.0.1",
                   allowed_ips: str = "0.0.0.0/0, ::/0", persistent_keepalive: int = 25) -> str:
    """Клиентский конфиг WireGuard для телефона. Ключи — с сервера (wg genkey / wg pubkey)."""
    return (
        "[Interface]\n"
        f"PrivateKey = {interface_privkey}\n"
        f"Address = {client_ip}\n"
        f"DNS = {dns}\n"
        "\n"
        "[Peer]\n"
        f"PublicKey = {server_pubkey}\n"
        f"Endpoint = {server_endpoint}\n"
        f"AllowedIPs = {allowed_ips}\n"
        f"PersistentKeepalive = {persistent_keepalive}\n"
    )


def wifi_http_proxy(cfg: dict, host_port: str | None, serial: str | None = None) -> None:
    """Глобальный http-прокси (только Wi-Fi): host:port, или None — сбросить. Служебный."""
    if host_port:
        device.shell(cfg, f"settings put global http_proxy {host_port}", serial=serial)
    else:
        device.shell(cfg, "settings delete global http_proxy", serial=serial)


def instructions() -> str:
    """Пошагово: как поднять прокси/VPN для телефона."""
    return """\
Как вывести телефон в нужную гео (Android 13, Galaxy A51, без root):

1. Мобильный прокси (предпочтительно, по одному на аккаунт):
   - арендуете мобильный 4G/5G-прокси нужной страны (липкий IP);
   - на телефоне ставите клиент по протоколу провайдера (обычно это готовое приложение
     с логином/паролем или конфиг HTTP/SOCKS5 через прокси-приложение, напр. v2rayNG/Clash);
   - включаете и проверяете гео (см. «Проверка» ниже).

2. Личный VPN на арендованном VPS (запасной, дешевле):
   - WireGuard: сервер wg genkey/wg pubkey, клиентский ключ; конфиг — phone.proxy.wireguard_conf();
     на телефон ставится приложение WireGuard (Play Store) и в него импортируется .conf;
   - Outline: ставите Outline Manager на сервере/локально, в Outline Client на телефоне
     импортируете ключ доступа. Всё остальное Outline делает сам.

Проверка: на телефоне в браузере открыть https://ipinfo.io или 2ip.ru — страна должна быть
нужной. Важно, чтобы гео не менялась между сессиями (липкий IP), иначе площадка видит скачки.

Один телефон = один аккаунт = один IP. Не заводите несколько аккаунтов через один VPN."""
