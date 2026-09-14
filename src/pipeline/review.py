"""Отбор сценариев перед озвучкой (веб-панель, python -m panel).

Сценарий после DeepSeek сохраняется со статусом meta.review.status = "pending" и в работу
не идёт. Одобренный — ставится в очередь цепочкой озвучка → (субтитры / интерфейс) → рендер,
отклонённый — остаётся на диске со статусом "rejected" (можно вернуть).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from core.scene import load_scene, output_dir, rel_path, save_scene, scene_path

log = logging.getLogger(__name__)

STATUSES = ("pending", "approved", "rejected")


class ReviewError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def processing_chain(scene: dict, cfg: dict, path: Path) -> list[dict]:
    """Цепочка задач от озвучки до готового ролика."""
    p = {"scene": rel_path(path)}
    chain = [{"kind": "tts_synthesis", "payload": p}]
    if scene["type"] in cfg["compositor"]["subtitles_for"]:
        chain.append({"kind": "subtitle_generation", "payload": p})
    if scene["type"] == "imessage_drama":
        chain.append({"kind": "ui_render", "payload": p})
    chain.append({"kind": "video_render", "payload": p})
    return chain


def mark_pending(scene: dict) -> dict:
    scene.setdefault("meta", {})["review"] = {"status": "pending", "created_at": _now()}
    return scene


def review_status(scene: dict) -> str | None:
    return scene.get("meta", {}).get("review", {}).get("status")


def list_scenes(cfg: dict, status: str | None = None) -> list[dict]:
    """Сцены, прошедшие через отбор (с meta.review), новые первыми."""
    folder = output_dir(cfg, "scenes")
    scenes = []
    for path in folder.glob("*.json") if folder.exists() else []:
        try:
            scene = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        current = review_status(scene)
        if current and (status is None or current == status):
            scenes.append(scene)
    return sorted(scenes, key=lambda s: s.get("created_at", ""), reverse=True)


def _load(cfg: dict, scene_id: str) -> tuple[dict, Path]:
    path = scene_path(cfg, scene_id)
    if not path.exists():
        raise ReviewError(f"Нет сцены {scene_id}")
    return load_scene(path), path


def set_texts(cfg: dict, scene_id: str, texts: list[str]) -> dict:
    """Правка текста реплик до озвучки. Пустой текст — реплика удаляется; роли не меняются."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError("Править можно только сценарий, ожидающий решения")
    if len(texts) != len(scene["script"]):
        raise ReviewError(f"Ожидалось {len(scene['script'])} реплик, пришло {len(texts)}")
    script = [{**line, "text": " ".join(text.split())} for line, text in zip(scene["script"], texts)
              if text.strip()]
    if not script:
        raise ReviewError("Сценарий не может быть пустым — отклоните его")
    scene["script"] = script
    scene["meta"]["review"]["edited_at"] = _now()
    save_scene(scene, path)
    return scene


def approve(cfg: dict, scene_id: str, queue) -> list[int]:
    """Одобрить: сцена уходит в очередь на озвучку и рендер. Возвращает id задач."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError(f"Сценарий уже {review_status(scene) or 'без отбора'}")
    scene["meta"]["review"].update(status="approved", decided_at=_now())
    save_scene(scene, path)
    ids = queue.enqueue_chain(processing_chain(scene, cfg, path))
    log.info("Одобрен «%s» → задачи %s", scene["meta"].get("title", scene_id), ids)
    return ids


def reject(cfg: dict, scene_id: str) -> dict:
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError(f"Сценарий уже {review_status(scene) or 'без отбора'}")
    scene["meta"]["review"].update(status="rejected", decided_at=_now())
    save_scene(scene, path)
    return scene


def restore(cfg: dict, scene_id: str) -> dict:
    """Вернуть отклонённый сценарий на проверку."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "rejected":
        raise ReviewError("Вернуть можно только отклонённый сценарий")
    scene["meta"]["review"] = {"status": "pending", "created_at": scene["meta"]["review"].get("created_at", _now())}
    save_scene(scene, path)
    return scene
