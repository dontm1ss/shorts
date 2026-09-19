"""Выгрузка роликов на площадки из командной строки (обычно это делается в панели).

    venv\\Scripts\\python -m upload status                      # ключи приложений и привязки учёток
    venv\\Scripts\\python -m upload link youtube --user admin   # ссылка на согласие (открыть в браузере)
    venv\\Scripts\\python -m upload unlink tiktok --user admin
    venv\\Scripts\\python -m upload send <scene_id> --to youtube --user admin
"""
import argparse
import logging
import sys

from core import config
from panel import users
from upload import PLATFORMS, UploadError, links, platform_module, upload_scene


def cmd_status(args, cfg) -> int:
    print(f"Адрес панели снаружи: {config.public_url(cfg)}")
    for platform in PLATFORMS:
        module = platform_module(platform)
        state = "ключи заданы" if module.configured() else "НЕТ ключей в .env"
        mode = f", режим {cfg['tiktok']['mode']}" if platform == "tiktok" else ""
        print(f"\n{platform}: {state}{mode}")
        print(f"  redirect для настроек приложения: {module.redirect_uri(cfg)}")
        linked = links.users_with(cfg, platform)
        if not linked:
            print("  привязок нет")
        for login in linked:
            link = links.get(cfg, login, platform) or {}
            name = link.get("channel_title") or link.get("display_name") or link.get("open_id", "")
            print(f"  {login} → {name} (привязан {link.get('linked_at', '?')})")
    return 0


def cmd_link(args, cfg) -> int:
    module = platform_module(args.platform)
    if not module.configured():
        print(f"В .env нет ключей приложения {args.platform}")
        return 1
    if not users.get(cfg, args.user):
        print(f"Нет учётки {args.user}")
        return 1
    state = f"{args.user}:cli"
    url = module.auth_url(cfg, state)
    verifier = None
    if isinstance(url, tuple):                       # TikTok возвращает ещё и code_verifier
        url, verifier = url
    print("Откройте в браузере и разрешите доступ:\n  " + url)
    if verifier:
        print("\nПривязка из командной строки для TikTok не доводится до конца: код возвращается\n"
              "на адрес панели. Привяжите аккаунт в личном кабинете панели.")
        return 1
    print("\nПосле согласия площадка вернёт браузер на адрес панели и привязка сохранится.")
    return 0


def cmd_unlink(args, cfg) -> int:
    module = platform_module(args.platform)
    if module.unlink_account(cfg, args.user):
        print(f"{args.platform} у {args.user} отвязан")
        return 0
    print(f"У {args.user} и не был привязан {args.platform}")
    return 1


def cmd_send(args, cfg) -> int:
    def progress(sent: int, total: int) -> None:
        print(f"\r  отправлено {sent / 2**20:.1f} из {total / 2**20:.1f} МБ", end="", flush=True)

    try:
        result = upload_scene(cfg, args.scene_id, args.to, args.user, progress)
    except (UploadError, RuntimeError) as e:
        print(f"\nНе отправлено: {e}")
        return 1
    print(f"\nГотово: {result.get('url') or result.get('publish_id', '')}")
    if args.to == "youtube" and result.get("privacy") == "private":
        print("Ролик на канале приватный — откройте его в YouTube Studio:\n  " + result.get("studio_url", ""))
    if args.to == "tiktok" and result.get("mode") == "inbox":
        print("Ролик в черновиках TikTok — допишите подпись и опубликуйте в приложении")
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m upload")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="ключи приложений и привязки учёток")
    p = sub.add_parser("link", help="ссылка на согласие площадки")
    p.add_argument("platform", choices=PLATFORMS)
    p.add_argument("--user", default="admin")
    p = sub.add_parser("unlink", help="отвязать площадку от учётки")
    p.add_argument("platform", choices=PLATFORMS)
    p.add_argument("--user", default="admin")
    p = sub.add_parser("send", help="отправить готовый ролик")
    p.add_argument("scene_id")
    p.add_argument("--to", choices=PLATFORMS, required=True)
    p.add_argument("--user", default="admin")
    args = parser.parse_args(argv)
    cfg = config.load_config()
    users.ensure_seed(cfg)
    return {"status": cmd_status, "link": cmd_link, "unlink": cmd_unlink, "send": cmd_send}[args.cmd](args, cfg)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    sys.exit(main())
