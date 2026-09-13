"""Сбор постов с Reddit.

Два источника:
- официальный API (OAuth, app-only) — если в .env заданы REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET:
  100 запросов в минуту, есть рейтинг, NSFW-флаг и тип поста;
- публичная RSS-лента (Atom) — без ключей. JSON-эндпоинты без OAuth отвечают 403, у RSS жёсткий
  лимит, поэтому пауза между запросами и повтор при 429.

Фильтры только технические (ТЗ 2.4): есть текст, не удалён, длина в пределах.
По лексике не фильтруем.
"""
from __future__ import annotations

import html
import logging
import re
import time
import xml.etree.ElementTree as ET

import requests

from scraper.common import html_to_text, reject_reason
from scraper.store import load_processed, save_post

log = logging.getLogger(__name__)

_ATOM = {"a": "http://www.w3.org/2005/Atom"}
_BODY = re.compile(r"<!-- SC_OFF -->(.*?)<!-- SC_ON -->", re.S)


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


def parse_listing(data: dict, subreddit: str) -> list[dict]:
    """Ответ API /r/<sub>/<sort> (Listing) → посты в общем формате."""
    posts = []
    for child in data.get("data", {}).get("children", []):
        p = child.get("data", {})
        posts.append({
            "id": f"reddit:{p.get('id')}",
            "source": "reddit",
            "subreddit": p.get("subreddit", subreddit),
            "title": p.get("title", ""),
            "text": p.get("selftext", ""),
            "url": "https://www.reddit.com" + p.get("permalink", ""),
            "score": p.get("score", 0),
            "over_18": bool(p.get("over_18")),
            "is_self": bool(p.get("is_self", True)),
            "stickied": bool(p.get("stickied")),
            "published": p.get("created_utc"),
        })
    return posts


def oauth_token(sc: dict, session: requests.Session) -> str:
    resp = session.post("https://www.reddit.com/api/v1/access_token",
                        auth=(sc["reddit_client_id"], sc["reddit_client_secret"]),
                        data={"grant_type": "client_credentials"}, timeout=30)
    if resp.status_code != 200 or "access_token" not in resp.json():
        raise RuntimeError(f"OAuth Reddit: HTTP {resp.status_code} {resp.text[:200]}")
    return resp.json()["access_token"]


def fetch_subreddit_api(subreddit: str, sc: dict, session: requests.Session, token: str) -> list[dict]:
    params = {"limit": sc["limit"], "raw_json": 1}
    if sc["sort"] == "top":
        params["t"] = sc["time_filter"]
    resp = session.get(f"https://oauth.reddit.com/r/{subreddit}/{sc['sort']}", params=params, timeout=30,
                       headers={"Authorization": f"bearer {token}"})
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    return parse_listing(resp.json(), subreddit)


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
    token = None
    if sc.get("reddit_client_id") and sc.get("reddit_client_secret"):
        try:
            token = oauth_token(sc, session)
            log.info("Reddit: официальный API (OAuth)")
        except (requests.RequestException, RuntimeError) as e:
            log.warning("OAuth Reddit не удался (%s) — перехожу на RSS", e)
    new_posts = []
    for i, sub in enumerate(subreddits or sc["subreddits"]):
        if i:
            time.sleep(1 if token else sc["request_delay_s"])
        try:
            posts = fetch_subreddit_api(sub, sc, session, token) if token else fetch_subreddit(sub, sc, session)
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
