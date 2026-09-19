"""Привязки площадок к учётным записям панели (`data/social-links.json`, в git не попадает).

Одна запись на пару «пользователь панели → площадка»: токены доступа и обновления, имя канала.
Токены обновляются и из панели, и из обработчика очереди, поэтому запись идёт атомарно и
поверх свежего файла — соседний процесс мог привязать другую площадку секунду назад.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from core.config import ROOT

PLATFORMS = ("youtube", "tiktok")
_lock = threading.Lock()


def links_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["social_links"]


def _load(cfg: dict) -> dict:
    try:
        data = json.loads(links_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cfg: dict, data: dict) -> None:
    path = links_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def get(cfg: dict, user: str, platform: str) -> dict | None:
    return (_load(cfg).get(user) or {}).get(platform)


def save(cfg: dict, user: str, platform: str, link: dict) -> None:
    """Привязка пользователя к площадке. Читаем файл заново под замком: пока шла загрузка,
    в нём могла появиться чужая привязка."""
    with _lock:
        data = _load(cfg)
        data.setdefault(user, {})[platform] = link
        _save(cfg, data)


def remove(cfg: dict, user: str, platform: str) -> bool:
    with _lock:
        data = _load(cfg)
        if platform not in (data.get(user) or {}):
            return False
        del data[user][platform]
        if not data[user]:
            del data[user]
        _save(cfg, data)
        return True


def of_user(cfg: dict, user: str) -> dict:
    """Все привязки пользователя: {платформа: запись}."""
    return _load(cfg).get(user) or {}


def users_with(cfg: dict, platform: str) -> list[str]:
    return [user for user, by_platform in _load(cfg).items() if platform in by_platform]
