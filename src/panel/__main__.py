"""Веб-панель отбора сценариев.

    venv\\Scripts\\python -m panel                  # http://127.0.0.1:8765
    venv\\Scripts\\python -m panel --port 9000

Наружу панель не открывается: только localhost, снаружи — через SSH-туннель на сервер
с HTTPS (scripts/panel_tunnel.ps1, docs/panel-deploy.md). Пароль — PANEL_PASSWORD в .env.
"""
import argparse
import logging
import sys

import uvicorn

from core.config import load_config
from panel.app import create_app

LOOPBACK = {"127.0.0.1", "localhost", "::1"}


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m panel")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    cfg = load_config(args.profile)
    host = args.host or cfg["panel"]["host"]
    port = args.port or cfg["panel"]["port"]
    if host not in LOOPBACK and not cfg["panel"].get("password"):
        logging.error("Панель на внешнем адресе без пароля не запускается — задайте PANEL_PASSWORD в .env")
        return 1
    logging.info("Панель: http://%s:%d  (профиль %s, пароль %s)", host, port, cfg["profile"]["name"],
                 "задан" if cfg["panel"].get("password") else "не задан — только localhost")
    uvicorn.run(create_app(cfg), host=host, port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
