"""CLI парсера: скачать новые посты в data/forum-posts/.

    venv\\Scripts\\python -m scraper [--sub tifu --sub pettyrevenge]
"""
import argparse
import logging
import sys

from core.config import load_config
from scraper import pending_posts, scrape


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m scraper")
    parser.add_argument("--sub", action="append", help="сабреддит (можно несколько раз)")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)
    cfg = load_config(args.profile)
    new = scrape(cfg, args.sub)
    logging.info("Новых постов: %d, всего неиспользованных: %d", len(new), len(pending_posts(cfg)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
