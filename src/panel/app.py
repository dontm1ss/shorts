"""Веб-панель: отбор сценариев перед озвучкой, запуск генерации, готовые ролики, очередь.

Работает на машине с конвейером (читает output/scenes, очередь SQLite, манифест).
Наружу — только через туннель на сервер с HTTPS и паролем (docs/panel-deploy.md).
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from core import config as core_config
from core.config import ROOT
from core.emotions import DEFAULT as DEFAULT_EMOTION, EMOTIONS
from core.scene import output_dir, rel_path, scene_path
from notify import notify
from panel import users
from panel.telegram_bot import FarmBot
from phone import PhoneError
from phone import accounts as phone_accounts
from pipeline import manifest
from pipeline.publish import load_publish
from pipeline.queue import WorkerLock
from pipeline.review import (ReviewError, approve, auto_approve, delete_scenes, finish_revision, list_scenes,
                             reject, restore, revising, set_auto_approve, set_texts, start_revision)
from pipeline.worker import open_queue
from scripting import estimate_duration_s, word_count
from upload import PLATFORMS, links, platform_module
from upload import history as upload_history

log = logging.getLogger(__name__)

WEB = Path(__file__).resolve().parent / "web"
STATIC = {"app.js": "text/javascript", "style.css": "text/css"}
COOKIE = "shorts_session"
OAUTH_TTL_S = 900            # столько живёт начатая привязка площадки (успеть согласиться в браузере)
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
    loop: bool = False   # переписка-петля: конец переходит в начало


class SettingsBody(BaseModel):
    auto_approve: bool


class LoginBody(BaseModel):
    login: str
    password: str


class PasswordBody(BaseModel):
    password: str


class NewUserBody(BaseModel):
    login: str
    password: str
    role: str = "user"


class UploadBody(BaseModel):
    platform: str


class AccountStatusBody(BaseModel):
    status: str
    notes: str | None = None
    handle: str | None = None


class ReviseBody(BaseModel):
    note: str                              # что изменить — своими словами
    texts: list[str] | None = None         # несохранённые правки карточки — применяются до переписывания
    emotions: list[str] | None = None


def _oauth_page(message: str, ok: bool = False) -> Response:
    """Страница, на которую площадка возвращает браузер после согласия: результат и путь назад
    в панель. Своего адреса у площадки нет — она умеет вернуть только на этот."""
    colour = "#1f8b4c" if ok else "#b3261e"
    body = (f"<h2 style='color:{colour}'>{'Готово' if ok else 'Не вышло'}</h2>"
            f"<p>{message}</p><p><a href='/'>Вернуться в панель</a></p>")
    return Response(f"<html><head><meta charset='utf-8'><title>shorts</title></head>"
                    f"<body style='font-family:system-ui;margin:3rem'>{body}</body></html>",
                    media_type="text/html")


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
        "revising": revising(scene),
        "score": meta.get("score"),
        "loop": bool(meta.get("loop")),
        "loop_idea": meta.get("loop_idea", ""),
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


def approved_time(meta: dict) -> str | None:
    """Когда сценарий одобрен (вручную или автоодобрением); у клипов отбора нет — None."""
    review = meta.get("review", {})
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


def revise_in_background(cfg: dict, scene_id: str, note: str) -> None:
    """Правка владельца → DeepSeek переписывает сценарий (и заново оценивает). Идёт в потоке панели:
    ответ DeepSeek может занять минуты, а запрос браузера с телефона столько не ждёт."""
    from scripting import DeepSeekClient, DeepSeekError, ScriptError, revise_scene, score_scene

    def revise(scene: dict) -> dict:
        client = DeepSeekClient(cfg)
        new = revise_scene(scene, note, client, cfg)
        if cfg["scoring"]["enabled"]:
            try:
                new["meta"]["score"] = score_scene(new, client, cfg)
            except (DeepSeekError, ScriptError) as e:
                log.warning("Оценить переписанный сценарий не удалось: %s", e)
        return new

    try:
        finish_revision(cfg, scene_id, revise)
    except Exception:   # сценарий удалили, пока DeepSeek отвечал, и т.п.
        log.exception("Переписывание по правке")


def ensure_worker(cfg: dict) -> int:
    """Есть задачи, а обработчик не запущен (упал, ПК перезагрузился) — запустить.
    Возвращает, сколько задач его ждало, или 0, если запускать не пришлось."""
    queue = open_queue(cfg)
    try:
        counts = queue.counts()
    finally:
        queue.close()
    waiting = counts.get("pending", 0) + counts.get("running", 0)   # running без воркера — после сбоя
    if not waiting or worker_running(cfg) or not start_worker(cfg):
        return 0
    return waiting


# --- действия панели и бота Telegram ------------------------------------------------------

def approve_and_start(cfg: dict, scene_id: str) -> tuple[list[int], bool]:
    """Одобрить сценарий: в очередь на озвучку и рендер, обработчик — запустить, если стоит."""
    queue = open_queue(cfg)
    try:
        ids = approve(cfg, scene_id, queue)
    finally:
        queue.close()
    return ids, start_worker(cfg)


def enqueue_once(cfg: dict, kind: str, scene_id: str) -> tuple[int, bool]:
    """Задача по сценарию готового ролика — продолжение (script_sequel) или тексты для публикации
    (publish_meta). Такая же ещё ждёт или идёт — вторая не ставится. Возвращает (id задачи, новая ли)."""
    path = scene_path(cfg, scene_id) if SCENE_ID.match(scene_id) else None
    if path is None or not path.is_file():
        raise ReviewError("Нет такого сценария")
    if not json.loads(path.read_text(encoding="utf-8")).get("script"):
        raise ReviewError("У клипа нет сценария")
    scene = rel_path(path)
    queue = open_queue(cfg)
    try:
        for status in ("pending", "running"):
            for job in queue.jobs(status, limit=100_000, kinds=(kind,)):
                if job.payload.get("scene") == scene:
                    return job.id, False
        return queue.enqueue(kind, {"scene": scene}), True
    finally:
        queue.close()


def enqueue_upload(cfg: dict, scene_id: str, platform: str, user: str) -> tuple[int, bool]:
    """Отправка ролика на площадку — задачей очереди. Такая же уже ждёт или идёт (тот же ролик,
    та же площадка) — вторая не ставится: одно лишнее нажатие не выложит ролик дважды."""
    queue = open_queue(cfg)
    try:
        for status in ("pending", "running"):
            for job in queue.jobs(status, limit=100_000, kinds=("social_upload",)):
                if job.payload.get("scene_id") == scene_id and job.payload.get("platform") == platform:
                    return job.id, False
        return queue.enqueue("social_upload", {"scene_id": scene_id, "platform": platform, "user": user}), True
    finally:
        queue.close()


def enqueue_phone(cfg: dict, kind: str, payload: dict) -> tuple[int, bool]:
    """Телефонная задача (прогрев или публикация) — задачей очереди. Такая же уже ждёт или идёт
    (совпадают все ключи payload) — вторая не ставится."""
    queue = open_queue(cfg)
    try:
        for status in ("pending", "running"):
            for job in queue.jobs(status, limit=100_000, kinds=(kind,)):
                if all(job.payload.get(k) == v for k, v in payload.items()):
                    return job.id, False
        return queue.enqueue(kind, payload), True
    finally:
        queue.close()


def scene_meta(cfg: dict, scene_id: str) -> dict:
    try:
        return json.loads(scene_path(cfg, scene_id).read_text(encoding="utf-8")).get("meta", {})
    except (OSError, ValueError):
        return {}


def _watchdog(cfg: dict, stop: threading.Event) -> None:
    """Сторож: обработчик не должен вставать молча — проверка сразу и раз в panel.watchdog_s."""
    while True:
        try:
            if waiting := ensure_worker(cfg):
                log.warning("Обработчик не работал при %d задачах в очереди — запущен", waiting)
                notify(cfg, f"⚠️ Обработчик очереди не работал — запущен снова. Задач в очереди: {waiting}")
        except Exception:
            log.exception("Сторож обработчика")
        if stop.wait(cfg["panel"]["watchdog_s"]):
            return


def create_app(cfg: dict) -> FastAPI:
    bot = FarmBot(cfg)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        stop = threading.Event()
        if cfg["panel"]["watchdog_s"]:
            threading.Thread(target=_watchdog, args=(cfg, stop), name="watchdog", daemon=True).start()
        if cfg["notify"]["enabled"] and cfg["notify"]["bot"]:
            threading.Thread(target=bot.run, args=(stop,), name="telegram", daemon=True).start()
        yield
        stop.set()

    app = FastAPI(title="shorts", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    basic_user, basic_password = cfg["panel"]["user"], cfg["panel"].get("password")
    created = users.ensure_seed(cfg)
    if created:
        log.info("Созданы учётки панели: %s — пароли из config [panel.seed], сменить в личном кабинете",
                 ", ".join(created))

    def basic_login(header: str) -> str | None:
        """Вход по HTTP Basic оставлен для скриптов и старых закладок: логин panel.user
        с PANEL_PASSWORD. Людям — вход по логину и паролю учётки."""
        if not (basic_password and header.startswith("Basic ")):
            return None
        try:
            got_user, _, got_pass = base64.b64decode(header[6:]).decode("utf-8").partition(":")
        except (ValueError, UnicodeDecodeError):
            return None
        ok = (secrets.compare_digest(got_user.encode(), basic_user.encode())
              and secrets.compare_digest(got_pass.encode(), basic_password.encode()))
        return basic_user if ok and users.get(cfg, basic_user) else None

    def current_user(request: Request) -> str | None:
        return (users.read_token(cfg, request.cookies.get(COOKIE, ""))
                or basic_login(request.headers.get("authorization", "")))

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        open_path = path in ("/", "/favicon.ico", "/api/login") or path.startswith("/static/")
        if not open_path:
            login = current_user(request)
            if not login:
                await asyncio.sleep(1)                   # замедлить перебор пароля
                return JSONResponse({"detail": "нужен вход"}, status_code=401)
            request.state.user = login
            request.state.role = (users.get(cfg, login) or {}).get("role", "user")
        # CSRF: браузер не пошлёт нестандартный заголовок с чужого сайта без CORS, а CORS мы не разрешаем
        if request.method in ("POST", "PUT", "DELETE") and request.headers.get("x-panel") != "1":
            return JSONResponse({"detail": "нет заголовка X-Panel"}, status_code=403)
        return await call_next(request)

    def require_admin(request: Request) -> str:
        if getattr(request.state, "role", "user") != "admin":
            raise HTTPException(status_code=403, detail="Нужны права администратора")
        return request.state.user

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
            "scoring": {"enabled": cfg["scoring"]["enabled"], "min_score": cfg["scoring"]["min_score"]},
            "telegram": bot.info(),
            "videos": len(manifest.entries(cfg)),
        }

    @app.get("/api/scenes")
    def scenes(status: str = "pending"):
        return [scene_card(s, cfg) for s in list_scenes(cfg, status)]

    @app.get("/api/scenes/{scene_id}")
    def one_scene(scene_id: str):
        path = scene_path(cfg, scene_id) if SCENE_ID.match(scene_id) else None
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="Нет такого сценария")
        return scene_card(json.loads(path.read_text(encoding="utf-8")), cfg)

    @app.post("/api/scenes/{scene_id}/revise", status_code=202)
    def revise_scene(scene_id: str, body: ReviseBody):
        """Переписать по правке: правки карточки сохраняются, DeepSeek работает фоном —
        карточка ждёт, пока revising не станет false."""
        try:
            if body.texts is not None:
                set_texts(cfg, scene_id, body.texts, body.emotions)
            start_revision(cfg, scene_id, body.note)
        except ReviewError as e:
            fail(e)
        threading.Thread(target=revise_in_background, args=(cfg, scene_id, " ".join(body.note.split())),
                         name=f"revise-{scene_id[:8]}", daemon=True).start()
        return {"status": "started"}

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
            jobs, started = approve_and_start(cfg, scene_id)
        except ReviewError as e:
            fail(e)
        return {"jobs": jobs, "worker_started": started}

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
                if body.loop:
                    raise HTTPException(status_code=400, detail="Петля есть только у переписок")
                ids = [queue.enqueue("script_story", {"topic": topic}) for _ in range(count)]
            elif body.kind == "drama":
                payload = {"topic": topic, "loop": True} if body.loop else {"topic": topic}
                ids = [queue.enqueue("script_drama", payload) for _ in range(count)]
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
        uploaded = upload_history.entries(cfg)
        result = []
        for entry in reversed(entries):
            file = output_dir(cfg, "videos", Path(entry.get("output", "")).name)
            scene_id = entry.get("scene_id", "")
            meta = scene_meta(cfg, scene_id) if SCENE_ID.match(scene_id) else {}
            approved_at = approved_time(meta)
            result.append({**entry, "url": f"/media/videos/{file.name}" if file.is_file() else None,
                           "size_mb": round(file.stat().st_size / 2**20, 1) if file.is_file() else None,
                           "approved_at": approved_at,
                           "elapsed_s": seconds_between(approved_at, entry.get("rendered_at")),
                           "work_s": round(work[scene_id]) if scene_id in work else None,
                           # тексты для публикации и продолжение — у роликов со сценарием (не у клипов)
                           "writable": entry.get("type") in ("reddit_story", "imessage_drama"),
                           "part": int(meta.get("part", 1)),
                           "publish": load_publish(cfg, scene_id) if SCENE_ID.match(scene_id) else None,
                           "uploads": uploaded.get(scene_id) or {}})
        return result

    @app.post("/api/videos/{scene_id}/upload")
    def upload_video(request: Request, scene_id: str, body: UploadBody):
        """Отправить готовый ролик на площадку от имени вошедшей учётки — задачей лёгкой линии,
        чтобы долгая загрузка не держала панель и переживала обрыв сети."""
        if body.platform not in PLATFORMS:
            raise HTTPException(status_code=404, detail="Неизвестная площадка")
        if not cfg[body.platform]["enabled"]:
            raise HTTPException(status_code=400, detail=f"Выгрузка на {body.platform} выключена в конфиге")
        if not links.get(cfg, request.state.user, body.platform):
            raise HTTPException(status_code=400,
                                detail=f"{body.platform} не привязан — личный кабинет")
        if manifest.find(cfg, scene_id) is None:
            raise HTTPException(status_code=404, detail="Нет такого ролика")
        job, new = enqueue_upload(cfg, scene_id, body.platform, request.state.user)
        return {"job": job, "new": new, "worker_started": start_worker(cfg) if new else False}

    @app.get("/api/accounts")
    def accounts_list():
        """Аккаунты площадок на телефоне: статус жизни, прогресс прогрева, история."""
        return [{"platform": p, **rec, "progress": phone_accounts.progress(cfg, rec)}
                for p, rec in phone_accounts.all(cfg).items()]

    @app.post("/api/accounts/{platform}/warmup")
    def account_warmup(platform: str):
        """Сессия прогрева аккаунта — задачей телефонной линии (adb + uiautomator2)."""
        if platform not in phone_accounts.platforms(cfg):
            raise HTTPException(status_code=404, detail="Нет такого аккаунта на телефоне")
        job, new = enqueue_phone(cfg, "phone_warmup", {"platform": platform})
        return {"job": job, "new": new, "worker_started": start_worker(cfg) if new else False}

    @app.post("/api/accounts/{platform}/status")
    def account_status(request: Request, platform: str, body: AccountStatusBody):
        """Ручная правка статуса/заметки/ника аккаунта (админ)."""
        require_admin(request)
        try:
            return phone_accounts.set_status(cfg, platform, body.status, body.notes, body.handle)
        except PhoneError as e:
            fail(e)

    @app.post("/api/videos/{scene_id}/phone-post")
    def phone_post_video(scene_id: str, body: UploadBody):
        """Опубликовать ролик через приложение на телефоне (TikTok / YouTube / Instagram) —
        задачей телефонной линии. В отличие от /upload не нужны привязки площадок."""
        if body.platform not in phone_accounts.platforms(cfg):
            raise HTTPException(status_code=404, detail="Нет такого аккаунта на телефоне")
        if manifest.find(cfg, scene_id) is None:
            raise HTTPException(status_code=404, detail="Нет такого ролика")
        job, new = enqueue_phone(cfg, "phone_post", {"scene_id": scene_id, "platform": body.platform})
        return {"job": job, "new": new, "worker_started": start_worker(cfg) if new else False}

    @app.post("/api/videos/{scene_id}/publish")
    def publish(scene_id: str):
        """Тексты для публикации ролика (подпись TikTok, заголовок и описание Shorts) — задачей лёгкой линии."""
        try:
            job, new = enqueue_once(cfg, "publish_meta", scene_id)
        except ReviewError as e:
            fail(e)
        return {"job": job, "new": new, "worker_started": start_worker(cfg)}

    @app.post("/api/videos/publish-missing")
    def publish_missing():
        """Тексты для публикации всем готовым роликам со сценарием, у которых их ещё нет."""
        new = 0
        for entry in manifest.entries(cfg):
            scene_id = entry.get("scene_id", "")
            if (entry.get("type") in ("reddit_story", "imessage_drama") and SCENE_ID.match(scene_id)
                    and load_publish(cfg, scene_id) is None):
                try:
                    new += enqueue_once(cfg, "publish_meta", scene_id)[1]
                except ReviewError:   # сцену удалили руками — ролик остался
                    continue
        return {"queued": new, "worker_started": start_worker(cfg) if new else False}

    @app.post("/api/videos/{scene_id}/sequel")
    def sequel(scene_id: str):
        """Продолжение ролика («Часть 2»): DeepSeek пишет следующую часть с теми же героями — на проверку."""
        try:
            job, new = enqueue_once(cfg, "script_sequel", scene_id)
        except ReviewError as e:
            fail(e)
        return {"job": job, "new": new, "worker_started": start_worker(cfg)}

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
        upload_history.forget(cfg, *deleted)
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
        upload_history.forget(cfg, scene_id)
        return {"deleted": Path(entry.get("output", "")).name}

    @app.delete("/api/scenes")
    def delete_scenes_by_status(status: str = "pending", ids: list[str] | None = Query(None)):
        try:
            return {"deleted": delete_scenes(cfg, status, ids)}
        except ReviewError as e:
            fail(e)

    # --- вход и личный кабинет ----------------------------------------------------

    pending_oauth: dict[str, dict] = {}       # state → кто и что привязывает (живёт OAUTH_TTL_S)

    def set_session(request: Request, response, login: str):
        """Кука сессии. Secure — по тому, как пришёл запрос (через Caddy это заголовок
        X-Forwarded-Proto): на локальной панели по http кука с Secure просто не вернулась бы."""
        proto = request.headers.get("x-forwarded-proto", request.url.scheme)
        response.set_cookie(COOKIE, users.make_token(cfg, login), httponly=True, samesite="lax",
                            max_age=int(cfg["panel"]["session_hours"] * 3600), secure=proto == "https")
        return response

    @app.post("/api/login")
    async def login(request: Request, body: LoginBody):
        account = users.authenticate(cfg, body.login.strip(), body.password)
        if not account:
            await asyncio.sleep(1)                       # замедлить перебор пароля
            raise HTTPException(status_code=401, detail="Неверный логин или пароль")
        log.info("Вход в панель: %s", account["login"])
        return set_session(request, JSONResponse(account), account["login"])

    @app.post("/api/logout")
    def logout():
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE)
        return response

    @app.get("/api/me")
    def me(request: Request):
        """Кто вошёл, что у него привязано и готовы ли площадки к работе вообще."""
        login = request.state.user
        mine = links.of_user(cfg, login)
        platforms = {}
        for platform in PLATFORMS:
            module = platform_module(platform)
            link = mine.get(platform) or {}
            platforms[platform] = {
                "enabled": bool(cfg[platform]["enabled"]),
                "configured": module.configured(),       # ключи приложения в .env
                "redirect_uri": module.redirect_uri(cfg),
                "linked": bool(link),
                "account": link.get("channel_title") or link.get("display_name") or link.get("username") or "",
                "linked_at": link.get("linked_at", ""),
            }
        return {"login": login, "role": request.state.role,
                "must_change_password": bool((users.get(cfg, login) or {}).get("must_change_password")),
                "platforms": platforms,
                "youtube_privacy": cfg["youtube"]["privacy_status"], "tiktok_mode": cfg["tiktok"]["mode"]}

    @app.post("/api/me/password")
    def change_password(request: Request, body: PasswordBody):
        try:
            users.set_password(cfg, request.state.user, body.password)
        except users.UserError as e:
            fail(e)
        log.info("Пароль сменён: %s", request.state.user)
        return {"ok": True}

    # --- привязка площадок --------------------------------------------------------

    @app.get("/api/social/{platform}/link")
    def social_link(request: Request, platform: str):
        """Начало привязки: ссылка на согласие площадки. Возврат — на /api/social/<platform>/callback."""
        if platform not in PLATFORMS:
            raise HTTPException(status_code=404, detail="Неизвестная площадка")
        module = platform_module(platform)
        if not module.configured():
            raise HTTPException(status_code=400, detail=f"В .env нет ключей приложения {platform}")
        state = secrets.token_urlsafe(24)
        try:
            url = module.auth_url(cfg, state)
        except RuntimeError as e:
            fail(e)
        verifier = None
        if isinstance(url, tuple):                       # TikTok отдаёт ещё и code_verifier (PKCE)
            url, verifier = url
        for old, rec in list(pending_oauth.items()):     # подчистить брошенные привязки
            if time.time() - rec["at"] > OAUTH_TTL_S:
                pending_oauth.pop(old, None)
        pending_oauth[state] = {"user": request.state.user, "platform": platform,
                                "verifier": verifier, "at": time.time()}
        return {"url": url}

    @app.get("/api/social/{platform}/callback")
    def social_callback(request: Request, platform: str, code: str = "", state: str = "", error: str = ""):
        """Возврат из браузера после согласия. Привязка сохраняется за той учёткой, которая её начала,
        — state сверяется с сохранённым, чужой ответ не принимается."""
        record = pending_oauth.pop(state, None)
        if error or not code:
            return _oauth_page(f"Доступ не выдан: {error or 'площадка не вернула код'}")
        if not record or record["platform"] != platform:
            return _oauth_page("Привязка не найдена или устарела — начните заново из личного кабинета")
        if record["user"] != request.state.user:
            return _oauth_page("Привязку начинала другая учётка — войдите под ней и повторите")
        module = platform_module(platform)
        try:
            link = (module.link_account(cfg, record["user"], code, record["verifier"]) if record["verifier"]
                    else module.link_account(cfg, record["user"], code))
        except RuntimeError as e:
            return _oauth_page(str(e))
        name = link.get("channel_title") or link.get("display_name") or link.get("username") or ""
        log.info("%s привязан к учётке %s (%s)", platform, record["user"], name)
        return _oauth_page(f"{platform} привязан: {name}".strip(), ok=True)

    @app.delete("/api/social/{platform}")
    def social_unlink(request: Request, platform: str):
        if platform not in PLATFORMS:
            raise HTTPException(status_code=404, detail="Неизвестная площадка")
        return {"unlinked": platform_module(platform).unlink_account(cfg, request.state.user)}

    # --- учётки (администратор) ---------------------------------------------------

    @app.get("/api/users")
    def all_users(request: Request):
        require_admin(request)
        linked = {platform: links.users_with(cfg, platform) for platform in PLATFORMS}
        return [{**u, "platforms": [p for p in PLATFORMS if u["login"] in linked[p]]}
                for u in users.list_users(cfg)]

    @app.post("/api/users")
    def create_user(request: Request, body: NewUserBody):
        require_admin(request)
        try:
            users.add_user(cfg, body.login, body.password, body.role)
        except users.UserError as e:
            fail(e)
        return {"created": body.login}

    @app.post("/api/users/{login}/password")
    def reset_password(request: Request, login: str, body: PasswordBody):
        require_admin(request)
        try:
            users.set_password(cfg, login, body.password)
        except users.UserError as e:
            fail(e)
        return {"ok": True}

    @app.delete("/api/users/{login}")
    def remove_user(request: Request, login: str):
        require_admin(request)
        if login == request.state.user:
            raise HTTPException(status_code=400, detail="Себя удалить нельзя")
        try:
            users.delete_user(cfg, login)
        except users.UserError as e:
            fail(e)
        for platform in PLATFORMS:
            links.remove(cfg, login, platform)
        return {"deleted": login}

    @app.get("/media/videos/{name}")
    def media(name: str):
        path = output_dir(cfg, "videos", name)
        if not VIDEO_NAME.match(name) or not path.is_file():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="video/mp4")

    return app
