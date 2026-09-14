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
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from core.config import ROOT
from core.scene import output_dir
from pipeline.queue import WorkerLock
from pipeline.review import ReviewError, approve, list_scenes, reject, restore, set_texts
from pipeline.worker import open_queue
from scripting import estimate_duration_s, word_count

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"app.js": "text/javascript", "style.css": "text/css"}
VIDEO_NAME = re.compile(r"^[\w.-]+\.mp4$")


class TextsBody(BaseModel):
    texts: list[str]


class GenerateBody(BaseModel):
    kind: str
    count: int = 1
    topic: str | None = None


def scene_card(scene: dict, cfg: dict) -> dict:
    meta = scene.get("meta", {})
    script = [{"speaker": line["speaker"], "text": line["text"]} for line in scene.get("script", [])]
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
            "queue": counts,
            "worker_running": worker_running(cfg),
            "timing": {"wps": s["words_per_second"], "line_overhead_s": s["line_overhead_s"],
                       "pause_s": cfg["tts"]["pause_between_lines_ms"] / 1000,
                       "min_s": s["min_duration_s"], "max_s": s["max_duration_s"]},
        }

    @app.get("/api/scenes")
    def scenes(status: str = "pending"):
        return [scene_card(s, cfg) for s in list_scenes(cfg, status)]

    @app.put("/api/scenes/{scene_id}/script")
    def save_texts(scene_id: str, body: TextsBody):
        try:
            return scene_card(set_texts(cfg, scene_id, body.texts), cfg)
        except ReviewError as e:
            fail(e)

    @app.post("/api/scenes/{scene_id}/approve")
    def approve_scene(scene_id: str, body: TextsBody | None = None):
        try:
            if body is not None:
                set_texts(cfg, scene_id, body.texts)
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
        path = ROOT / cfg["paths"]["manifest"]
        entries = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        result = []
        for entry in reversed(entries[-200:]):
            file = output_dir(cfg, "videos", Path(entry.get("output", "")).name)
            result.append({**entry, "url": f"/media/videos/{file.name}" if file.is_file() else None,
                           "size_mb": round(file.stat().st_size / 2**20, 1) if file.is_file() else None})
        return result

    @app.get("/media/videos/{name}")
    def media(name: str):
        path = output_dir(cfg, "videos", name)
        if not VIDEO_NAME.match(name) or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    return app
