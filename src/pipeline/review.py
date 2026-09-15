"""Отбор сценариев перед озвучкой (веб-панель, python -m panel).

Сценарий после DeepSeek сохраняется со статусом meta.review.status = "pending" и в работу
не идёт. Одобренный — ставится в очередь цепочкой озвучка → (субтитры / интерфейс) → рендер,
отклонённый — остаётся на диске со статусом "rejected" (можно вернуть).
Автоодобрение (переключатель в панели) — новые сценарии сразу одобряются и идут в работу.
"""
from __future__ import annotations

import json
import logging
import os
from collections.abc import Collection
from datetime import datetime, timezone
from pathlib import Path

from core.emotions import DEFAULT as DEFAULT_EMOTION, EMOTIONS
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


def mark_auto_approved(scene: dict) -> dict:
    now = _now()
    scene.setdefault("meta", {})["review"] = {"status": "approved", "auto": True, "created_at": now, "decided_at": now}
    return scene


def mark_rejected(scene: dict, reason: str) -> dict:
    """Отклонён без владельца (оценка редактора ниже порога) — виден в «Отклонённых», можно вернуть."""
    now = _now()
    scene.setdefault("meta", {})["review"] = {"status": "rejected", "auto": True, "reason": reason,
                                              "created_at": now, "decided_at": now}
    return scene


REVISE_STALE_S = 600   # «переписывается» дольше — панель упала посреди запроса, отметка не действует


def revising(scene: dict) -> bool:
    """DeepSeek сейчас переписывает сценарий по правке владельца (панель, фоном)."""
    started = scene.get("meta", {}).get("review", {}).get("revising")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() < REVISE_STALE_S
    except (TypeError, ValueError):
        return False


def _not_revising(scene: dict) -> None:
    if revising(scene):
        raise ReviewError("DeepSeek ещё переписывает этот сценарий — подождите")


def _state_path(cfg: dict) -> Path:
    return output_dir(cfg, "review-state.json")


def auto_approve(cfg: dict) -> bool:
    """Включено ли автоодобрение: переключатель панели, по умолчанию — review.auto_approve."""
    try:
        return bool(json.loads(_state_path(cfg).read_text(encoding="utf-8"))["auto_approve"])
    except (OSError, ValueError, KeyError, TypeError):
        return bool(cfg["review"].get("auto_approve", False))


def set_auto_approve(cfg: dict, enabled: bool) -> bool:
    """Переключатель читают задачи сценариев — они идут отдельными процессами, поэтому файл."""
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"auto_approve": bool(enabled), "changed_at": _now()}), encoding="utf-8")
    os.replace(tmp, path)
    log.info("Автоодобрение %s", "включено" if enabled else "выключено")
    return bool(enabled)


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


def set_texts(cfg: dict, scene_id: str, texts: list[str], emotions: list[str] | None = None) -> dict:
    """Правка текста и интонации (эмоции) реплик до озвучки. Пустой текст — реплика удаляется;
    роли не меняются. emotions=None — эмоции остаются как были."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError("Править можно только сценарий, ожидающий решения")
    _not_revising(scene)
    if len(texts) != len(scene["script"]):
        raise ReviewError(f"Ожидалось {len(scene['script'])} реплик, пришло {len(texts)}")
    if emotions is None:
        emotions = [line.get("emotion", DEFAULT_EMOTION) for line in scene["script"]]
    if len(emotions) != len(texts):
        raise ReviewError(f"Ожидалось {len(texts)} эмоций, пришло {len(emotions)}")
    if unknown := sorted(set(emotions) - set(EMOTIONS)):
        raise ReviewError(f"Неизвестная эмоция: {', '.join(unknown)}")
    script = [{**line, "text": " ".join(text.split()), "emotion": emotion}
              for line, text, emotion in zip(scene["script"], texts, emotions) if text.strip()]
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
    _not_revising(scene)
    scene["meta"]["review"].update(status="approved", decided_at=_now())
    save_scene(scene, path)
    ids = queue.enqueue_chain(processing_chain(scene, cfg, path))
    log.info("Одобрен «%s» → задачи %s", scene["meta"].get("title", scene_id), ids)
    return ids


def reject(cfg: dict, scene_id: str) -> dict:
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError(f"Сценарий уже {review_status(scene) or 'без отбора'}")
    _not_revising(scene)
    scene["meta"]["review"].update(status="rejected", decided_at=_now())
    save_scene(scene, path)
    return scene


def start_revision(cfg: dict, scene_id: str, note: str) -> dict:
    """Отметить, что сценарий переписывается по правке владельца; сам запрос к DeepSeek идёт фоном
    (finish_revision). Пока идёт — править, одобрять и отклонять нельзя."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "pending":
        raise ReviewError("Переписать можно только сценарий, ожидающий решения")
    _not_revising(scene)
    if not " ".join(note.split()):
        raise ReviewError("Напишите, что изменить")
    review = scene["meta"]["review"]
    review["revising"] = _now()
    review.pop("revise_error", None)
    save_scene(scene, path)
    return scene


def finish_revision(cfg: dict, scene_id: str, revise) -> dict:
    """revise(сцена) → переписанная сцена (DeepSeek). Сохраняется, пока сценарий ждёт решения;
    ошибка — в meta.review.revise_error, сценарий остаётся прежним."""
    scene, path = _load(cfg, scene_id)
    try:
        new = revise(scene)
    except Exception as e:
        log.warning("Переписать «%s» по правке не удалось: %s", scene["meta"].get("title", scene_id), e)
        new, error = None, str(e)[:300]
    scene, path = _load(cfg, scene_id)   # за время запроса сценарий могли удалить — тогда ReviewError
    review = {k: v for k, v in scene["meta"]["review"].items() if k not in ("revising", "revise_error")}
    if new is None or review.get("status") != "pending":
        scene["meta"]["review"] = review if new else {**review, "revise_error": error}
        save_scene(scene, path)
        return scene
    new["meta"]["review"] = {**review, "revised_at": _now()}
    save_scene(new, path)
    log.info("«%s» переписан по правке", new["meta"].get("title", scene_id))
    return new


def delete_scenes(cfg: dict, status: str, ids: Collection[str] | None = None) -> int:
    """Удалить насовсем сценарии на проверке или отклонённые (ids — только эти из них). Одобренные
    не трогаются — по ним идут задачи озвучки и рендера. Возвращает, сколько удалено."""
    if status not in ("pending", "rejected"):
        raise ReviewError("Удалять можно только сценарии на проверке или отклонённые")
    scenes = [s for s in list_scenes(cfg, status) if ids is None or s["scene_id"] in ids]
    for scene in scenes:
        scene_path(cfg, scene["scene_id"]).unlink(missing_ok=True)
    if scenes:
        log.info("Удалено сценариев (%s): %d", status, len(scenes))
    return len(scenes)


def restore(cfg: dict, scene_id: str) -> dict:
    """Вернуть отклонённый сценарий на проверку."""
    scene, path = _load(cfg, scene_id)
    if review_status(scene) != "rejected":
        raise ReviewError("Вернуть можно только отклонённый сценарий")
    scene["meta"]["review"] = {"status": "pending", "created_at": scene["meta"]["review"].get("created_at", _now())}
    save_scene(scene, path)
    return scene
