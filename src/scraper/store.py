"""Хранилище постов и дедупликация (data/processed-forum-ids.json)."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from core.config import ROOT


def _processed_file(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["processed_forum_ids"]


def posts_dir(cfg: dict) -> Path:
    return ROOT / cfg["scraper"]["posts_dir"]


def load_processed(cfg: dict) -> set[str]:
    path = _processed_file(cfg)
    if not path.exists():
        return set()
    return set(json.loads(path.read_text(encoding="utf-8")))


def mark_processed(cfg: dict, post_id: str) -> None:
    ids = load_processed(cfg) | {post_id}
    path = _processed_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)   # атомарно: файл не бьётся при падении посреди записи


def post_file(cfg: dict, post_id: str) -> Path:
    return posts_dir(cfg) / (re.sub(r"[^\w.-]+", "_", post_id) + ".json")


def save_post(cfg: dict, post: dict) -> Path:
    path = post_file(cfg, post["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def pending_posts(cfg: dict) -> list[Path]:
    """Сохранённые, но ещё не использованные посты — лучшие по рейтингу первыми."""
    folder = posts_dir(cfg)
    if not folder.exists():
        return []
    done = load_processed(cfg)
    posts = []
    for path in folder.glob("*.json"):
        post = json.loads(path.read_text(encoding="utf-8"))
        if post["id"] not in done:
            posts.append((post.get("score", 0), path))
    return [p for _, p in sorted(posts, key=lambda x: -x[0])]
