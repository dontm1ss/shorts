"""Учётные записи панели (`data/users.json`, в git не попадает) и сессии в подписанной куке.

Пароли хранятся солёными хэшами (PBKDF2-HMAC-SHA256), не открытым текстом: файл лежит на машине
фермы, а панель смотрит наружу через туннель. Первый запуск создаёт учётки из config `[panel.seed]`
— их пароли нужно сменить в личном кабинете.

Сессия — кука с подписью HMAC на секрете из `data/panel-secret` (создаётся сам): панель можно
перезапускать, не разлогинивая всех, а подделать её без файла секрета нельзя.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from core.config import ROOT

ITERATIONS = 240_000
ROLES = ("admin", "user")
_lock = threading.Lock()


class UserError(ValueError):
    pass


# --- пароли ---------------------------------------------------------------------

def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, rounds, salt, digest = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        want = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), base64.b64decode(salt), int(rounds))
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(want, base64.b64decode(digest))


# --- хранилище ------------------------------------------------------------------

def users_path(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["users"]


def _load(cfg: dict) -> dict:
    try:
        data = json.loads(users_path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(cfg: dict, data: dict) -> None:
    path = users_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def ensure_seed(cfg: dict) -> list[str]:
    """Первый запуск: создаём учётки из конфига. Уже существующие не трогаем — сменённый
    пароль не откатывается обратно к заводскому при перезапуске панели."""
    seed = cfg["panel"].get("seed") or {}
    with _lock:
        data = _load(cfg)
        created = []
        for login, spec in seed.items():
            if login in data:
                continue
            data[login] = {"password": hash_password(str(spec["password"])),
                           "role": spec.get("role", "user"),
                           "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "must_change_password": True}
            created.append(login)
        if created:
            _save(cfg, data)
    return created


def list_users(cfg: dict) -> list[dict]:
    return [{"login": login, "role": rec.get("role", "user"), "created_at": rec.get("created_at", ""),
             "must_change_password": bool(rec.get("must_change_password"))}
            for login, rec in sorted(_load(cfg).items())]


def get(cfg: dict, login: str) -> dict | None:
    return _load(cfg).get(login)


def authenticate(cfg: dict, login: str, password: str) -> dict | None:
    record = _load(cfg).get(login)
    if not record or not verify_password(password, record.get("password", "")):
        return None
    return {"login": login, "role": record.get("role", "user"),
            "must_change_password": bool(record.get("must_change_password"))}


def add_user(cfg: dict, login: str, password: str, role: str = "user") -> None:
    login = login.strip()
    if not login.isascii() or not login.replace("_", "").replace("-", "").isalnum():
        raise UserError("Логин — латиница, цифры, дефис и подчёркивание")
    if len(password) < 6:
        raise UserError("Пароль короче 6 знаков")
    if role not in ROLES:
        raise UserError(f"Роль — одна из: {', '.join(ROLES)}")
    with _lock:
        data = _load(cfg)
        if login in data:
            raise UserError(f"Учётка {login} уже есть")
        data[login] = {"password": hash_password(password), "role": role,
                       "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "must_change_password": True}
        _save(cfg, data)


def set_password(cfg: dict, login: str, password: str) -> None:
    if len(password) < 6:
        raise UserError("Пароль короче 6 знаков")
    with _lock:
        data = _load(cfg)
        if login not in data:
            raise UserError(f"Нет учётки {login}")
        data[login]["password"] = hash_password(password)
        data[login]["must_change_password"] = False
        _save(cfg, data)


def delete_user(cfg: dict, login: str) -> None:
    with _lock:
        data = _load(cfg)
        if login not in data:
            raise UserError(f"Нет учётки {login}")
        if data[login].get("role") == "admin" and sum(r.get("role") == "admin" for r in data.values()) == 1:
            raise UserError("Это последний администратор — удалять некому будет управлять панелью")
        del data[login]
        _save(cfg, data)


# --- сессии ---------------------------------------------------------------------

def _secret(cfg: dict) -> bytes:
    """Секрет подписи кук: создаётся при первом обращении и живёт рядом с учётками."""
    path = ROOT / cfg["paths"]["panel_secret"]
    if path.exists():
        return path.read_bytes()
    with _lock:
        if path.exists():                      # успел создать соседний поток
            return path.read_bytes()
        path.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_bytes(32)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_bytes(secret)
        os.replace(tmp, path)
        return secret


def make_token(cfg: dict, login: str) -> str:
    """Кука сессии: логин + срок + подпись. Хранить сессии в памяти нельзя — панель
    перезапускается сторожем, и все оказались бы разлогинены."""
    expires = int(time.time() + cfg["panel"]["session_hours"] * 3600)
    payload = f"{login}:{expires}".encode("utf-8")
    body = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    sign = hmac.new(_secret(cfg), payload, hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sign}"


def read_token(cfg: dict, token: str) -> str | None:
    """Логин из куки или None — подпись не сошлась, срок вышел или учётку удалили."""
    try:
        body, _, sign = (token or "").partition(".")
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        login, _, expires = payload.decode("utf-8").rpartition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    want = hmac.new(_secret(cfg), payload, hashlib.sha256).hexdigest()[:32]
    if not secrets.compare_digest(want, sign) or not expires.isdigit() or int(expires) < time.time():
        return None
    return login if get(cfg, login) else None
