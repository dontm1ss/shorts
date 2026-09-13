"""Сбор постов с Pikabu.

Ключ/OAuth не нужен — единственный анонимный эндпоинт `xmlfeeds.php` отдаёт общую ленту
"популярное" без авторизации (RSS 2.0, windows-1251). У Pikabu нет официального API;
параметры вида `cmd=new`/`cmd=best`/`tag=`/`community=` без входа в аккаунт сервер
игнорирует и всегда отдаёт один и тот же общий фид — так что сообщества/сортировку
(как у Reddit — список сабреддитов) выбрать нельзя, только общий поток.

В отличие от Reddit, в ленте нет рейтинга, флага 18+ и признака текстового поста —
рейтинг заменяем местом в топе (как и в RSS Reddit), 18+ ищем по «NSFW»/«18+» в
заголовке (сама лента для гостя такое обычно не показывает — эвристика на всякий
случай), is_self всегда True — отсекать посты-картинки без текста будет фильтр длины.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET

import requests

from scraper.common import html_to_text, reject_reason
from scraper.store import load_processed, save_post

log = logging.getLogger(__name__)

FEED_URL = "https://pikabu.ru/xmlfeeds.php?cmd=popular"
_ID = re.compile(r"_(\d+)/?$")
_NSFW = re.compile(r"\bnsfw\b|\b18\+", re.I)


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items = root.findall("channel/item")
    posts = []
    for rank, item in enumerate(items):
        link = item.findtext("link", "") or item.findtext("guid", "")
        m = _ID.search(link)
        title = html.unescape(item.findtext("title", "") or "")
        posts.append({
            "id": f"pikabu:{m.group(1) if m else link}",
            "source": "pikabu",
            "title": title,
            "text": html_to_text(item.findtext("description", "") or ""),
            "url": link,
            "score": len(items) - rank,               # в ленте нет рейтинга — место в топе
            "over_18": bool(_NSFW.search(title)),
            "is_self": True,
            "stickied": False,
            "published": item.findtext("pubDate", "") or "",
        })
    return posts


def fetch_feed(session: requests.Session) -> list[dict]:
    resp = session.get(FEED_URL, timeout=30)
    if resp.status_code != 200 or not resp.content:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return parse_feed(resp.content)


def scrape(cfg: dict) -> list[dict]:
    """Скачивает новые подходящие посты из общей ленты Pikabu в data/forum-posts/."""
    sc = cfg["scraper"]
    done = load_processed(cfg)
    session = requests.Session()
    session.headers["User-Agent"] = sc["user_agent"]
    try:
        posts = fetch_feed(session)
    except (requests.RequestException, RuntimeError, ET.ParseError) as e:
        log.warning("Pikabu: пропускаю (%s)", e)
        return []
    new_posts = []
    rejected: dict[str, int] = {}
    for post in posts:
        reason = "уже использован" if post["id"] in done else reject_reason(post, sc)
        if reason:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        save_post(cfg, post)
        new_posts.append(post)
    log.info("Pikabu: %d постов, подошло %d, отсеяно: %s", len(posts),
             len(posts) - sum(rejected.values()), rejected or "—")
    return new_posts
