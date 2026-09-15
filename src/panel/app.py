"""Веб-панель: отбор сценариев перед озвучкой, запуск генерации, готовые ролики, очередь.

Работает на машине с конвейером (читает output/scenes, очередь SQLite, манифест).
Наружу — только через туннель на сервер с HTTPS и паролем (docs/panel-deploy.md).
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from core.config import ROOT
from core.emotions import DEFAULT as DEFAULT_EMOTION, EMOTIONS
from core.scene import output_dir, scene_path
from pipeline import manifest
from pipeline.queue import WorkerLock
from pipeline.review import (ReviewError, approve, auto_approve, delete_scenes, list_scenes, reject, restore,
                             set_auto_approve, set_texts)
from pipeline.worker import open_queue
from scripting import estimate_duration_s, word_count

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"app.js": "text/javascript", "style.css": "text/css"}
VIDEO_NAME = re.compile(r"^[\w.-]+\.mp4$")
SCENE_ID = re.compile(r"^[\w-]{1,80}$")
PROCESSING_KINDS = ("tts_synthesis", "subtitle_generation", "ui_render", "video_render")


class TextsBody(BaseModel):
    texts: list[str]
    emotions: list[str] | None = None


class GenerateBody(BaseModel):
    kind: str
    count: int = 1
    topic: str | None = None


class SettingsBody(BaseModel):
    auto_approve: bool


def scene_card(scene: dict, cfg: dict) -> dict:
    meta = scene.get("meta", {})
    script = [{"speaker": line["speaker"], "text": line["text"], "emotion": line.get("emotion", DEFAULT_EMOTION)}
              for line in scene.get("script", [])]
    return {
        "scene_id": scene["scene_id"],
        "type": scene["type"],
        "title": meta.get("title", ""),
        "created_at": scene.get("created_at", ""),
        "source": meta.get("source") or {},
        "topic": meta.get("topic", ""),
        "contacts": meta.get("contacts", {}),
        "genders": meta.get("genders", {}),
        "review": meta.get("review", {}),
        "script": script,
        "words": sum(word_count(line["text"]) for line in script),
        "est_s": round(estimate_duration_s(script, cfg), 1) if script else 0,
    }


def delete_video_files(cfg: dict, entry: dict) -> bool:
    """Файл ролика и промежуточные файлы сцены (озвучка). False — файл занят плеером."""
    name = Path(entry.get("output", "")).name
    file = output_dir(cfg, "videos", name)
    for attempt in range(4):
        try:
            if VIDEO_NAME.match(name) and file.is_file():
                file.unlink()
            break
        except PermissionError:   # Windows: файл ещё отдаётся плееру — страница его уже отпустила
            if attempt == 3:
                return False
            time.sleep(0.5)
    if SCENE_ID.match(scene_id := entry.get("scene_id", "")):
        shutil.rmtree(output_dir(cfg, "work", scene_id), ignore_errors=True)
    return True


def approved_time(cfg: dict, scene_id: str) -> str | None:
    """Когда сценарий одобрен (вручную или автоодобрением); у клипов отбора нет — None."""
    if not SCENE_ID.match(scene_id):
        return None
    try:
        scene = json.loads(scene_path(cfg, scene_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    review = scene.get("meta", {}).get("review", {})
    return review.get("decided_at") if review.get("status") == "approved" else None


def seconds_between(start: str | None, end: str | None) -> int | None:
    try:
        return round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):   # нет одной из отметок или время без пояса
        return None


def work_seconds(cfg: dict) -> dict[str, float]:
    """Чистое время обработки каждой сцены — сумма длительностей задач озвучки, субтитров,
    записи интерфейса и рендера, без ожидания в очереди."""
    queue = open_queue(cfg)
    try:
        jobs = queue.jobs("done", limit=1_000_000, kinds=PROCESSING_KINDS)
    finally:
        queue.close()
    work: dict[str, float] = {}
    for job in jobs:
        spent = seconds_between(job.started_at, job.finished_at)
        if spent is not None and "scene" in job.payload:
            scene_id = Path(job.payload["scene"]).stem
            work[scene_id] = work.get(scene_id, 0) + spent
    return work


def worker_running(cfg: dict) -> bool:
    lock = WorkerLock(output_dir(cfg, "queue.lock"))
    if lock.acquire():
        lock.release()
        return False
    return True


def start_worker(cfg: dict) -> bool:
    """Воркер очереди в фоне (--watch), если ещё не запущен."""
    if worker_running(cfg):
        return False
    log_path = output_dir(cfg, "logs", "worker.log")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "SHORTS_PROFILE": cfg["profile"]["name"]}
    # Windows: своя скрытая консоль (CREATE_NO_WINDOW), а не отсутствие консоли (DETACHED_PROCESS) —
    # иначе дочерние задачи воркера открывали бы видимые окна; своя группа — не падать вместе с панелью
    kwargs = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
              if sys.platform == "win32" else {"start_new_session": True})
    with open(log_path, "a", encoding="utf-8") as fh:
        subprocess.Popen([sys.executable, "-m", "pipeline", "worker", "--watch"], cwd=ROOT, env=env,
                         stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **kwargs)
    return True


def create_app(cfg: dict) -> FastAPI:
    app = FastAPI(title="shorts", docs_url=None, redoc_url=None, openapi_url=None)
    user, password = cfg["panel"]["user"], cfg["panel"].get("password")

    def authorized(header: str) -> bool:
        if not header.startswith("Basic "):
            return False
        try:
            got_user, _, got_pass = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return (secrets.compare_digest(got_user.encode(), user.encode())
                and secrets.compare_digest(got_pass.encode(), password.encode()))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if password and not authorized(request.headers.get("authorization", "")):
            await asyncio.sleep(1)                       # замедлить перебор пароля
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="shorts", charset="UTF-8"'})
        # CSRF: браузер не пошлёт нестандартный заголовок с чужого сайта без CORS, а CORS мы не разрешаем
        if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-panel") != "1":
            return JSONResponse({"detail": "нет заголовка X-Panel"}, status_code=403)
        return await call_next(request)

    def fail(e: Exception):
        raise HTTPException(status_code=400, detail=str(e))

    # --- страница -----------------------------------------------------------------

    @app.get("/")
    def index():
        return FileResponse(WEB / "index.html", media_type="text/html")

    @app.get("/static/{name}")
    def static(name: str):
        if name not in STATIC:
            raise HTTPException(status_code=404)
        return FileResponse(WEB / name, media_type=STATIC[name])

    # --- API ----------------------------------------------------------------------

    @app.get("/api/summary")
    def summary():
        queue = open_queue(cfg)
        counts = queue.counts()
        queue.close()
        s = cfg["story"]
        return {
            "profile": cfg["profile"]["name"],
            "review": {st: len(list_scenes(cfg, st)) for st in ("pending", "approved", "rejected")},
            "review_enabled": cfg["review"]["enabled"],
            "auto_approve": auto_approve(cfg),
            "queue": counts,
            "worker_running": worker_running(cfg),
            "timing": {"wps": s["words_per_second"], "line_overhead_s": s["line_overhead_s"],
                       "pause_s": cfg["tts"]["pause_between_lines_ms"] / 1000,
                       "min_s": s["min_duration_s"], "max_s": s["max_duration_s"]},
            "emotions": [{"id": key, "label": label} for key, (label, _) in EMOTIONS.items()],
        }

    @app.get("/api/scenes")
    def scenes(status: str = "pending"):
        return [scene_card(s, cfg) for s in list_scenes(cfg, status)]

    @app.put("/api/scenes/{scene_id}/script")
    def save_texts(scene_id: str, body: TextsBody):
        try:
            return scene_card(set_texts(cfg, scene_id, body.texts, body.emotions), cfg)
        except ReviewError as e:
            fail(e)

    @app.post("/api/scenes/{scene_id}/approve")
    def approve_scene(scene_id: str, body: TextsBody | None = None):
        try:
            if body is not None:
                set_texts(cfg, scene_id, body.texts, body.emotions)
            queue = open_queue(cfg)
            try:
                jobs = approve(cfg, scene_id, queue)
            finally:
                queue.close()
        except ReviewError as e:
            fail(e)
        return {"jobs": jobs, "worker_started": start_worker(cfg)}

    @app.post("/api/scenes/{scene_id}/reject")
    def reject_scene(scene_id: str):
        try:
            return scene_card(reject(cfg, scene_id), cfg)
        except ReviewError as e:
            fail(e)

    @app.post("/api/scenes/{scene_id}/restore")
    def restore_scene(scene_id: str):
        try:
            return scene_card(restore(cfg, scene_id), cfg)
        except ReviewError as e:
            fail(e)

    @app.post("/api/generate")
    def generate(body: GenerateBody):
        count = max(1, min(body.count, 20))
        queue = open_queue(cfg)
        try:
            topic = (body.topic or "").strip() or None
            if body.kind == "story":
                ids = [queue.enqueue("script_story", {"topic": topic}) for _ in range(count)]
            elif body.kind == "drama":
                ids = [queue.enqueue("script_drama", {"topic": topic}) for _ in range(count)]
            else:
                raise HTTPException(status_code=400, detail="kind: story | drama")
        finally:
            queue.close()
        return {"jobs": ids, "worker_started": start_worker(cfg)}

    @app.put("/api/settings")
    def settings(body: SettingsBody):
        return {"auto_approve": set_auto_approve(cfg, body.auto_approve)}

    @app.post("/api/worker/start")
    def worker_start():
        return {"started": start_worker(cfg), "running": True}

    @app.get("/api/jobs")
    def jobs():
        queue = open_queue(cfg)
        items = []
        for j in reversed(queue.jobs(limit=60)):
            error_lines = (j.error or "").strip().splitlines()
            items.append({"id": j.id, "kind": j.kind, "status": j.status, "attempts": j.attempts,
                          "created_at": j.created_at, "started_at": j.started_at, "finished_at": j.finished_at,
                          "error": error_lines[-1] if error_lines else ""})
        queue.close()
        return items

    @app.get("/api/videos")
    def videos():
        entries = manifest.entries(cfg)[-200:]
        work = work_seconds(cfg)
        result = []
        for entry in reversed(entries):
            file = output_dir(cfg, "videos", Path(entry.get("output", "")).name)
            approved_at = approved_time(cfg, entry.get("scene_id", ""))
            result.append({**entry, "url": f"/media/videos/{file.name}" if file.is_file() else None,
                           "size_mb": round(file.stat().st_size / 2**20, 1) if file.is_file() else None,
                           "approved_at": approved_at,
                           "elapsed_s": seconds_between(approved_at, entry.get("rendered_at")),
                           "work_s": round(work[entry["scene_id"]]) if entry.get("scene_id") in work else None})
        return result

    @app.delete("/api/videos")
    def delete_all_videos(ids: list[str] | None = Query(None)):
        """Удалить готовые ролики — все или ids (те, что видны на странице: новые, отрендеренные
        после её загрузки, не трогаются). Занятые плеером файлы остаются в списке."""
        deleted, busy = [], 0
        for entry in manifest.entries(cfg):
            if ids is not None and entry.get("scene_id") not in ids:
                continue
            if delete_video_files(cfg, entry):
                deleted.append(entry.get("scene_id"))
            else:
                busy += 1
        manifest.remove(cfg, *deleted)
        return {"deleted": len(deleted), "busy": busy}

    @app.delete("/api/videos/{scene_id}")
    def delete_video(scene_id: str):
        """Удалить готовый ролик: файл, запись в списке и промежуточные файлы сцены (озвучку)."""
        entry = manifest.find(cfg, scene_id) if SCENE_ID.match(scene_id) else None
        if entry is None:
            raise HTTPException(status_code=404, detail="Нет такого ролика")
        if not delete_video_files(cfg, entry):
            raise HTTPException(status_code=409, detail="Файл занят — остановите видео и попробуйте ещё раз")
        manifest.remove(cfg, scene_id)
        return {"deleted": Path(entry.get("output", "")).name}

    @app.delete("/api/scenes")
    def delete_scenes_by_status(status: str = "pending", ids: list[str] | None = Query(None)):
        try:
            return {"deleted": delete_scenes(cfg, status, ids)}
        except ReviewError as e:
            fail(e)

    @app.get("/media/videos/{name}")
    def media(name: str):
        path = output_dir(cfg, "videos", name)
        if not VIDEO_NAME.match(name) or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    return app
