"""Управление телефоном из командной строки.

    venv\\Scripts\\python -m phone devices                     # подключённые устройства
    venv\\Scripts\\python -m phone warmup [--seed 1]           # сессия прогрева TikTok
    venv\\Scripts\\python -m phone post <scene_id> --to tiktok # опубликовать ролик
    venv\\Scripts\\python -m phone screenshot [out.png]        # снимок экрана
    venv\\Scripts\\python -m phone proxy                       # как поднять прокси/VPN
"""
import argparse
import logging
import sys

from core import config
from phone import PhoneError, accounts, device, post, proxy, warmup
from phone.selectors import SELECTORS


def cmd_devices(args, cfg) -> int:
    for d in device.devices(cfg):
        extra = " ".join(f"{k}={v}" for k, v in d.items() if k not in ("serial", "state"))
        print(f"{d['serial']}  {d['state']}  {extra}".rstrip())
    return 0


def cmd_accounts(args, cfg) -> int:
    for platform, rec in accounts.all(cfg).items():
        p = accounts.progress(cfg, rec)
        status = accounts.STATUS_LABEL.get(rec["status"], rec["status"])
        print(f"{platform:10} {status:14} сессий {p['sessions']}/{p['sessions_to_warm']} ({p['percent']}%), "
              f"постов {rec.get('posts', 0)}, последний прогрев: {rec.get('last_warmup_at') or '—'}")
    return 0


def cmd_warmup(args, cfg) -> int:
    result = warmup.run(cfg, args.platform, args.serial, seed=args.seed)
    print(f"Прогрев {result['platform']}: {result['actions']} действий, {result['planned_s']} с, "
          f"контент: {result['keyword'] or 'лента'}, статус аккаунта: {result['status']}.")
    return 0


def cmd_post(args, cfg) -> int:
    result = post.post(cfg, args.scene_id, args.to, caption=args.caption, serial=args.serial)
    print(f"Опубликовано: {result['platform']} ← {result['remote']}")
    return 0


def cmd_screenshot(args, cfg) -> int:
    out = device.screenshot(cfg, args.out, serial=args.serial)
    print(out)
    return 0


def cmd_proxy(args, cfg) -> int:
    if args.http:
        proxy.wifi_http_proxy(cfg, None if args.http == "off" else args.http, serial=args.serial)
        print("http-прокси сброшен" if args.http == "off" else f"http-прокси: {args.http}")
        return 0
    print(proxy.instructions())
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m phone")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="подключённые устройства")
    sub.add_parser("accounts", help="статусы аккаунтов и прогрева")

    p = sub.add_parser("warmup", help="сессия прогрева аккаунта")
    p.add_argument("--platform", choices=list(SELECTORS), default="tiktok")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--serial", default=None)

    p = sub.add_parser("post", help="опубликовать готовый ролик")
    p.add_argument("scene_id")
    p.add_argument("--to", choices=list(SELECTORS), required=True)
    p.add_argument("--caption", default=None)
    p.add_argument("--serial", default=None)

    p = sub.add_parser("screenshot", help="снимок экрана")
    p.add_argument("out", nargs="?", default="output/phone/screen.png")
    p.add_argument("--serial", default=None)

    p = sub.add_parser("proxy", help="как поднять прокси/VPN на телефоне")
    p.add_argument("--http", metavar="HOST:PORT", default=None,
                   help="глобальный http-прокси (Wi-Fi), 'off' — сбросить")
    p.add_argument("--serial", default=None)

    args = parser.parse_args(argv)
    cfg = config.load_config()
    try:
        return {"devices": cmd_devices, "accounts": cmd_accounts, "warmup": cmd_warmup,
                "post": cmd_post, "screenshot": cmd_screenshot, "proxy": cmd_proxy}[args.cmd](args, cfg)
    except PhoneError as e:
        print(f"Телефон: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
