"""Что и куда уже отправлено (`output/uploads.json`): одна запись на пару «ролик → площадка».

Панель показывает по ней состояние ролика («на YouTube», «в черновиках TikTok») и не даёт
отправить одно и то же дважды по случайному второму нажатию.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from core.config import ROOT

_lock = threading.Lock()


def history_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["uploads"]


def _load(cfg: dict) -> dict:
    try:
        data = json.loads(history_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cfg: dict, data: dict) -> None:
    path = history_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def record(cfg: dict, scene_id: str, platform: str, result: dict) -> None:
    """Загрузку пишет обработчик очереди, пока панель читает файл, — запись атомарная
    и поверх свежего содержимого."""
    with _lock:
        data = _load(cfg)
        data.setdefault(scene_id, {})[platform] = result
        _save(cfg, data)


def of_scene(cfg: dict, scene_id: str) -> dict:
    return _load(cfg).get(scene_id) or {}


def forget(cfg: dict, *scene_ids: str) -> None:
    """Забыть отправки удалённых роликов (ролик удалили из панели)."""
    with _lock:
        data = _load(cfg)
        if any(data.pop(scene_id, None) is not None for scene_id in scene_ids):
            _save(cfg, data)


def entries(cfg: dict) -> dict:
    return _load(cfg)
