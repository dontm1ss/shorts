"""Сбор постов с Reddit через публичную RSS-ленту (Atom), без авторизации.

JSON-эндпоинты Reddit без OAuth отвечают 403, RSS доступен, но с жёстким лимитом —
поэтому пауза между запросами и повтор при 429. Для больших объёмов нужен OAuth-клиент.

Фильтры только технические (ТЗ 2.4): есть текст, не удалён, длина в пределах.
По лексике не фильтруем.
"""
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import requests

from scraper.store import load_processed, save_post

log = logging.getLogger(__name__)

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_BODY = re.compile(r"<!-- SC_OFF -->(.*?)<!-- SC_ON -->", re.S)
_BLOCK_TAGS = {"p", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "tr", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n" if tag != "li" else "\n- ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def parse_feed(xml_bytes: bytes, subreddit: str) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    posts = []
    entries = root.findall("a:entry", _ATOM)
    for rank, entry in enumerate(entries):
        raw_id = entry.findtext("a:id", "", _ATOM)
        content = entry.findtext("a:content", "", _ATOM)
        body = _BODY.search(content)
        link = entry.find("a:link", _ATOM)
        category = entry.find("a:category", _ATOM)
        title = html.unescape(entry.findtext("a:title", "", _ATOM))
        posts.append({
            "id": f"reddit:{raw_id.removeprefix('t3_')}",
            "source": "reddit",
            "subreddit": category.get("term") if category is not None else subreddit,
            "title": title,
            "text": html_to_text(body.group(1)) if body else "",
            "url": link.get("href") if link is not None else "",
            "score": len(entries) - rank,          # в RSS нет рейтинга — место в топе
            "over_18": bool(re.search(r"\bnsfw\b", title, re.I)),
            "published": entry.findtext("a:published", "", _ATOM),
        })
    return posts


def reject_reason(post: dict, sc: dict) -> str | None:
    text = post["text"].strip()
    if text in ("", "[removed]", "[deleted]"):
        return "нет текста"
    if not sc["min_chars"] <= len(text) <= sc["max_chars"]:
        return "длина"
    if sc["exclude_nsfw"] and post["over_18"]:
        return "nsfw"
    return None


def fetch_subreddit(subreddit: str, sc: dict, session: requests.Session, retries: int = 3) -> list[dict]:
    url = f"https://www.reddit.com/r/{subreddit}/{sc['sort']}/.rss"
    params = {"t": sc["time_filter"], "limit": sc["limit"]} if sc["sort"] == "top" else {"limit": sc["limit"]}
    for attempt in range(retries):
        resp = session.get(url, params=params, timeout=30)
        if resp.status_code == 429:
            wait = int(resp.headers.get("Retry-After", 0) or 0) or 30 * (attempt + 1)
            log.info("r/%s: лимит Reddit (429), жду %d с", subreddit, wait)
            time.sleep(wait)
            continue
        if resp.status_code != 200 or not resp.content:
            raise RuntimeError(f"HTTP {resp.status_code}")
        return parse_feed(resp.content, subreddit)
    raise RuntimeError("лимит запросов Reddit (429) не снялся")


def scrape(cfg: dict, subreddits: list[str] | None = None) -> list[dict]:
    """Скачивает новые подходящие посты в data/forum-posts/. Возвращает новые."""
    sc = cfg["scraper"]
    done = load_processed(cfg)
    session = requests.Session()
    session.headers["User-Agent"] = sc["user_agent"]
    new_posts = []
    for i, sub in enumerate(subreddits or sc["subreddits"]):
        if i:
            time.sleep(sc["request_delay_s"])
        try:
            posts = fetch_subreddit(sub, sc, session)
        except (requests.RequestException, RuntimeError, ET.ParseError) as e:
            log.warning("Пропускаю r/%s: %s", sub, e)
            continue
        rejected: dict[str, int] = {}
        for post in posts:
            reason = "уже использован" if post["id"] in done else reject_reason(post, sc)
            if reason:
                rejected[reason] = rejected.get(reason, 0) + 1
                continue
            save_post(cfg, post)
            new_posts.append(post)
        log.info("r/%s: %d постов, подошло %d, отсеяно: %s", sub, len(posts),
                 len(posts) - sum(rejected.values()), rejected or "—")
    return new_posts
