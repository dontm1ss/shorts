"""Тексты для публикации готовых роликов (подпись TikTok, заголовок и описание Shorts, хэштеги):
output/publish/<scene_id>.json. Отдельно от сцены — сцену в это время пишут задачи озвучки и рендера."""
from __future__ import annotations

import json
import os
from pathlib import Path

from core.scene import output_dir


def publish_path(cfg: dict, scene_id: str) -> Path:
    return output_dir(cfg, "publish", f"{scene_id}.json")


def load_publish(cfg: dict, scene_id: str) -> dict | None:
    try:
        return json.loads(publish_path(cfg, scene_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_publish(cfg: dict, scene_id: str, texts: dict) -> None:
    path = publish_path(cfg, scene_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(texts, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)
