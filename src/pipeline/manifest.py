"""Манифест готовых роликов (`output/manifest.json`): заголовок, формат, источник,
длительность, путь к файлу — одна запись на каждый video_render.

Без этого при десятках роликов в день узнать, что внутри `reddit_story_0577e97c.mp4`,
можно только открыв `output/scenes/<id>.json`. Манифест даёт список для отсмотра/выбора
без этого."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from core.config import ROOT


def _manifest_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["manifest"]


def _label(scene: dict) -> dict:
    """title/source/url — по-разному устроены в каждом формате сцены."""
    meta = scene.get("meta", {})
    if scene["type"] == "reddit_story":
        source = meta.get("source") or {}
        return {"title": meta.get("title", ""), "source": source.get("source", ""), "url": source.get("url", "")}
    if scene["type"] == "imessage_drama":
        return {"title": meta.get("title", ""), "source": "deepseek", "url": meta.get("topic", "")}
    sv = scene.get("source_video") or {}
    return {"title": "", "source": sv.get("path", ""), "url": ""}


def record(cfg: dict, scene: dict, result: dict) -> None:
    """Добавляет запись о готовом ролике. Атомарная запись, как data/processed-forum-ids.json."""
    path = _manifest_path(cfg)
    entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    entries.append({
        "scene_id": scene["scene_id"],
        "type": scene["type"],
        **_label(scene),
        "duration_s": round(result.get("duration_s", 0.0), 1),
        "output": result.get("output", ""),
        "created_at": scene.get("created_at", ""),
        "rendered_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
