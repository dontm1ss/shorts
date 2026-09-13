from scraper.common import html_to_text, reject_reason
from scraper.pikabu import scrape as scrape_pikabu
from scraper.reddit import parse_feed, parse_listing
from scraper.reddit import scrape as scrape_reddit
from scraper.store import load_processed, mark_processed, pending_posts, save_post


def scrape(cfg: dict, subreddits: list[str] | None = None) -> list[dict]:
    """Собирает новые посты со всех источников: Reddit (RSS или официальный API, если
    в .env есть ключи) + Pikabu (без ключа — единственная анонимная общая лента)."""
    return scrape_reddit(cfg, subreddits) + scrape_pikabu(cfg)


__all__ = ["html_to_text", "load_processed", "mark_processed", "parse_feed", "parse_listing",
           "pending_posts", "reject_reason", "save_post", "scrape", "scrape_pikabu", "scrape_reddit"]
