"""Аккаунты площадок на телефоне: статус «жизни» и прогрева (`data/phone-accounts.json`).

Один телефон = один аккаунт на площадку (TikTok + YouTube Shorts + Instagram Reels), см.
docs/phone-automation.md. Здесь хранится состояние каждого аккаунта, чтобы панель и бот
показывали, на каком он этапе и когда его последний раз прогревали.

Статусы:
  new      — аккаунт создан, прогрева ещё не было;
  warming  — идёт прогрев (набирает сессии/секунды до warmed);
  warmed   — прогрет, готов публиковать;
  active   — уже публикует ролики;
  paused   — на паузе (вручную);
  banned   — забанен (вручную).

Переходы автоматические: первая сессия прогрева переводит new → warming, а набрав
`phone.warmup.sessions_to_warm` сессий (или `seconds_to_warm` секунд) — warming → warmed.
Первая публикация переводит warmed → active. Вручную (админ) можно выставить любой статус.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from core.config import ROOT
from phone import PhoneError

_lock = threading.Lock()

STATUSES = ("new", "warming", "warmed", "active", "paused", "banned")
STATUS_LABEL = {"new": "новый", "warming": "прогревается", "warmed": "прогрет",
                "active": "активен", "paused": "пауза", "banned": "бан"}
# Статусы, из которых ручной/автоматический прогрев уже не «понижает» аккаунт
SETTLED = ("warmed", "active", "paused", "banned")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def accounts_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["phone_accounts"]


def platforms(cfg: dict) -> list[str]:
    return list(cfg["phone"]["warmup"].get("platforms") or ("tiktok", "youtube", "instagram"))


def _default(platform: str) -> dict:
    return {"platform": platform, "handle": "", "status": "new", "created_at": _now(),
            "warmed_at": "", "last_warmup_at": "", "last_post_at": "", "warmup_sessions": 0,
            "warmup_seconds": 0, "posts": 0, "notes": "",
            "history": []}


def _load(cfg: dict) -> dict:
    try:
        data = json.loads(accounts_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cfg: dict, data: dict) -> None:
    path = accounts_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def all(cfg: dict) -> dict[str, dict]:
    """Записи всех настроенных площадок (недостающие создаются на лету)."""
    with _lock:
        data = _load(cfg)
        changed = False
        for platform in platforms(cfg):
            if platform not in data:
                data[platform] = _default(platform)
                changed = True
        if changed:
            _save(cfg, data)
        return {platform: data[platform] for platform in platforms(cfg)}


def get(cfg: dict, platform: str) -> dict:
    return all(cfg)[platform]


def set_status(cfg: dict, platform: str, status: str, notes: str | None = None,
               handle: str | None = None) -> dict:
    """Ручная правка статуса/заметки/ника (админ)."""
    if status not in STATUSES:
        raise PhoneError(f"Неизвестный статус: {status}")
    with _lock:
        data = _load(cfg)
        rec = data.setdefault(platform, _default(platform))
        rec["status"] = status
        if status == "warmed" and not rec.get("warmed_at"):
            rec["warmed_at"] = _now()
        if notes is not None:
            rec["notes"] = notes.strip()
        if handle is not None:
            rec["handle"] = handle.strip()
        _save(cfg, data)
        return rec


def record_warmup(cfg: dict, platform: str, seconds: float, actions: int = 0) -> dict:
    """Записать сессию прогрева: счётчики, отметка времени и автоматический статус."""
    w = cfg["phone"]["warmup"]
    with _lock:
        data = _load(cfg)
        rec = data.setdefault(platform, _default(platform))
        rec["warmup_sessions"] = int(rec.get("warmup_sessions", 0)) + 1
        rec["warmup_seconds"] = int(rec.get("warmup_seconds", 0)) + int(seconds)
        rec["last_warmup_at"] = _now()
        rec.setdefault("history", []).append({"at": rec["last_warmup_at"], "kind": "warmup",
                                              "seconds": int(seconds), "actions": int(actions)})
        rec["history"] = rec["history"][-50:]
        if rec["status"] not in SETTLED:
            warmed = (rec["warmup_sessions"] >= int(w["sessions_to_warm"])
                      or rec["warmup_seconds"] >= int(w["seconds_to_warm"]))
            rec["status"] = "warmed" if warmed else "warming"
            if warmed and not rec.get("warmed_at"):
                rec["warmed_at"] = _now()
        _save(cfg, data)
        return rec


def record_post(cfg: dict, platform: str, scene_id: str = "") -> dict:
    """Записать публикацию: warmed → active, счётчик постов."""
    with _lock:
        data = _load(cfg)
        rec = data.setdefault(platform, _default(platform))
        rec["posts"] = int(rec.get("posts", 0)) + 1
        rec["last_post_at"] = _now()
        rec.setdefault("history", []).append({"at": rec["last_post_at"], "kind": "post",
                                              "scene_id": scene_id})
        rec["history"] = rec["history"][-50:]
        if rec["status"] in ("new", "warming", "warmed"):
            rec["status"] = "active"
        _save(cfg, data)
        return rec


def progress(cfg: dict, rec: dict) -> dict:
    """Сколько аккаунт уже прогрелся: для полоски в панели/боте."""
    w = cfg["phone"]["warmup"]
    sessions, need = int(rec.get("warmup_sessions", 0)), max(1, int(w["sessions_to_warm"]))
    seconds, need_s = int(rec.get("warmup_seconds", 0)), max(1, int(w["seconds_to_warm"]))
    done = rec.get("status") in SETTLED
    percent = 100 if done else min(100, round(max(sessions / need, seconds / need_s) * 100))
    return {"sessions": sessions, "sessions_to_warm": need,
            "seconds": seconds, "seconds_to_warm": need_s, "percent": percent, "done": done}
