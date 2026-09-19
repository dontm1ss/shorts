"""TikTok: привязка аккаунта и отправка готовых роликов (Content Posting API v2, Login Kit).

Ключи приложения — в .env (TIKTOK_CLIENT_KEY, TIKTOK_CLIENT_SECRET; developers.tiktok.com,
разрешённый redirect — `<адрес панели>/api/social/tiktok/callback`). Аккаунт привязывается
в личном кабинете панели, токены — `data/social-links.json` (upload.links).

Два режима (tiktok.mode):
  inbox  — ролик уходит в черновики TikTok, автор дописывает и публикует сам из приложения.
           Работает без аудита приложения и позволяет публиковать публично — режим по умолчанию.
  direct — прямая публикация. У приложения без аудита TikTok разрешает только SELF_ONLY
           («видно мне»), да ещё и требует, чтобы аккаунт на момент отправки был приватным,
           поэтому до аудита этот режим смысла почти не имеет.

Ограничения TikTok: 6 запросов в минуту на токен и не больше 5 незавершённых отправок за сутки
на аккаунт — на ферме это узкое место, а не скорость загрузки.

Без сторонних библиотек (как notify/telegram.py): requests уже в зависимостях.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
import time
import urllib.parse
from pathlib import Path

import requests

from core import config
from upload import links

log = logging.getLogger(__name__)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
REVOKE_URL = "https://open.tiktokapis.com/v2/oauth/revoke/"
USER_URL = "https://open.tiktokapis.com/v2/user/info/"
CREATOR_URL = "https://open.tiktokapis.com/v2/post/publish/creator_info/query/"
INBOX_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/inbox/video/init/"
DIRECT_INIT_URL = "https://open.tiktokapis.com/v2/post/publish/video/init/"
STATUS_URL = "https://open.tiktokapis.com/v2/post/publish/status/fetch/"
PROFILE_URL = "https://www.tiktok.com/@{name}"

SCOPES_INBOX = ("user.info.basic", "video.upload")
SCOPES_DIRECT = ("user.info.basic", "video.publish")
TITLE_MAX = 2200
SINGLE_CHUNK_MAX = 64 * 2**20     # TikTok принимает один кусок до 64 МБ — наши ролики меньше (render.max_size_mb)


class TikTokError(RuntimeError):
    pass


def credentials() -> tuple[str | None, str | None]:
    env = config.read_env_file()
    value = lambda name: (os.environ.get(name) or env.get(name) or "").strip().strip("<>").strip() or None  # noqa: E731
    return value("TIKTOK_CLIENT_KEY"), value("TIKTOK_CLIENT_SECRET")


def configured() -> bool:
    return all(credentials())


def scopes(cfg: dict) -> tuple[str, ...]:
    return SCOPES_DIRECT if cfg["tiktok"]["mode"] == "direct" else SCOPES_INBOX


# --- привязка аккаунта ----------------------------------------------------------

def redirect_uri(cfg: dict) -> str:
    return config.public_url(cfg).rstrip("/") + "/api/social/tiktok/callback"


def auth_url(cfg: dict, state: str) -> tuple[str, str]:
    """Ссылка на согласие TikTok и code_verifier к ней (PKCE обязателен для веб-приложений).
    Проверочную строку кладём в сессию и подставляем при обмене кода."""
    client_key, _ = credentials()
    if not client_key:
        raise TikTokError("В .env нет TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET")
    verifier = secrets.token_urlsafe(64)
    challenge = hashlib.sha256(verifier.encode()).hexdigest()   # TikTok ждёт hex, не base64url
    query = urllib.parse.urlencode({
        "client_key": client_key, "response_type": "code", "scope": ",".join(scopes(cfg)),
        "redirect_uri": redirect_uri(cfg), "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256"})
    return f"{AUTH_URL}?{query}", verifier


def _token_request(payload: dict) -> dict:
    try:
        resp = requests.post(TOKEN_URL, data=payload, timeout=30,
                             headers={"Content-Type": "application/x-www-form-urlencoded"})
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise TikTokError(f"TikTok не ответил на запрос токена: {e}") from e
    if data.get("error") or resp.status_code != 200:
        raise TikTokError(f"TikTok отказал в токене: {data.get('error_description') or data.get('error') or data}")
    return data


def _profile(token: str) -> dict:
    try:
        resp = requests.get(USER_URL, params={"fields": "display_name,username"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        user = ((resp.json().get("data") or {}).get("user")) or {}
    except (requests.RequestException, ValueError):
        return {}
    return {"display_name": user.get("display_name", ""), "username": user.get("username", "")}


def _store(cfg: dict, user: str, data: dict, extra: dict | None = None) -> dict:
    link = {"access_token": data["access_token"], "refresh_token": data.get("refresh_token", ""),
            "expires_at": time.time() + int(data.get("expires_in", 86400)),
            "refresh_expires_at": time.time() + int(data.get("refresh_expires_in", 365 * 86400)),
            "open_id": data.get("open_id", ""), "scope": data.get("scope", ""),
            **(extra or {})}
    links.save(cfg, user, "tiktok", link)
    return link


def link_account(cfg: dict, user: str, code: str, verifier: str) -> dict:
    client_key, client_secret = credentials()
    data = _token_request({"client_key": client_key, "client_secret": client_secret, "code": code,
                           "grant_type": "authorization_code", "redirect_uri": redirect_uri(cfg),
                           "code_verifier": verifier})
    return _store(cfg, user, data, {"linked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                    **_profile(data["access_token"])})


def unlink_account(cfg: dict, user: str) -> bool:
    link = links.get(cfg, user, "tiktok")
    if link:
        client_key, client_secret = credentials()
        try:
            requests.post(REVOKE_URL, timeout=15,
                          data={"client_key": client_key, "client_secret": client_secret,
                                "token": link.get("access_token", "")})
        except requests.RequestException as e:
            log.warning("TikTok: отозвать токен не удалось (%s) — привязка убрана только у нас", e)
    return links.remove(cfg, user, "tiktok")


def access_token(cfg: dict, user: str) -> str:
    """Действующий access-токен (живёт сутки). Refresh-токен TikTok — год, но тоже протухает."""
    client_key, client_secret = credentials()
    if not (client_key and client_secret):
        raise TikTokError("В .env нет TIKTOK_CLIENT_KEY и TIKTOK_CLIENT_SECRET")
    link = links.get(cfg, user, "tiktok")
    if not link:
        raise TikTokError("Аккаунт TikTok не привязан — личный кабинет → «Привязать TikTok»")
    if link.get("access_token") and link.get("expires_at", 0) > time.time() + 60:
        return link["access_token"]
    if not link.get("refresh_token") or link.get("refresh_expires_at", 0) < time.time():
        raise TikTokError("Привязка TikTok устарела — привяжите аккаунт заново")
    data = _token_request({"client_key": client_key, "client_secret": client_secret,
                           "refresh_token": link["refresh_token"], "grant_type": "refresh_token"})
    keep = {k: link.get(k, "") for k in ("linked_at", "display_name", "username")}
    return _store(cfg, user, data, keep)["access_token"]


# --- отправка ролика ------------------------------------------------------------

def _api(url: str, token: str, payload: dict, timeout: float = 60) -> dict:
    try:
        resp = requests.post(url, json=payload, timeout=timeout,
                             headers={"Authorization": f"Bearer {token}",
                                      "Content-Type": "application/json; charset=UTF-8"})
        body = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise TikTokError(f"TikTok не ответил ({url.rsplit('/', 2)[-2]}): {e}") from e
    error = body.get("error") or {}
    if error.get("code") not in (None, "ok"):
        raise TikTokError(f"TikTok отказал: {error.get('message') or error.get('code')}")
    return body.get("data") or {}


def creator_info(cfg: dict, user: str) -> dict:
    """Что TikTok разрешает этому автору: доступные уровни приватности, лимит длительности,
    выключены ли дуэты/стич. Прямая публикация без этого запроса запрещена правилами."""
    return _api(CREATOR_URL, access_token(cfg, user), {})


def _privacy_level(cfg: dict, info: dict) -> str:
    """Уровень приватности для прямой публикации: из конфига, если TikTok его разрешает,
    иначе — самый закрытый из разрешённых (у приложения без аудита это SELF_ONLY)."""
    allowed = info.get("privacy_level_options") or []
    wanted = cfg["tiktok"]["privacy_level"]
    if wanted in allowed:
        return wanted
    for level in ("SELF_ONLY", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "PUBLIC_TO_EVERYONE"):
        if level in allowed:
            return level
    raise TikTokError("TikTok не вернул ни одного разрешённого уровня приватности")


def _init(cfg: dict, user: str, title: str, size: int) -> dict:
    """Начало отправки: TikTok выдаёт publish_id и адрес, куда класть файл."""
    token = access_token(cfg, user)
    source = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": size, "total_chunk_count": 1}
    if cfg["tiktok"]["mode"] == "direct":
        t = cfg["tiktok"]
        post_info = {"title": title[:TITLE_MAX], "privacy_level": _privacy_level(cfg, creator_info(cfg, user)),
                     "disable_duet": bool(t["disable_duet"]), "disable_comment": bool(t["disable_comment"]),
                     "disable_stitch": bool(t["disable_stitch"]),
                     "video_cover_timestamp_ms": int(t["cover_timestamp_ms"])}
        return _api(DIRECT_INIT_URL, token, {"post_info": post_info, "source_info": source})
    # inbox: подпись задаёт автор в приложении — TikTok её здесь не принимает
    return _api(INBOX_INIT_URL, token, {"source_info": source})


def upload(cfg: dict, user: str, video: Path, title: str, progress=None) -> dict:
    """Отправка ролика в TikTok. Ролики фермы ≤ render.max_size_mb, поэтому файл уходит одним
    куском — TikTok разрешает до 64 МБ за раз."""
    video = Path(video)
    if not video.is_file():
        raise TikTokError(f"Нет файла ролика: {video}")
    size = video.stat().st_size
    if size > SINGLE_CHUNK_MAX:
        raise TikTokError(f"Ролик {size / 2**20:.0f} МБ — больше 64 МБ TikTok одним куском не принимает")
    data = _init(cfg, user, title, size)
    publish_id, url = data.get("publish_id", ""), data.get("upload_url", "")
    if not url:
        raise TikTokError("TikTok не вернул адрес для загрузки файла")
    log.info("TikTok: загрузка %s (%.1f МБ) от имени %s, режим %s",
             video.name, size / 2**20, user, cfg["tiktok"]["mode"])
    body = video.read_bytes()
    last = None
    for attempt in range(cfg["tiktok"]["max_retries"] + 1):
        try:
            resp = requests.put(url, data=body, timeout=600,
                                headers={"Content-Type": "video/mp4", "Content-Length": str(size),
                                         "Content-Range": f"bytes 0-{size - 1}/{size}"})
        except requests.RequestException as e:
            last = str(e)
        else:
            if resp.status_code in (200, 201):
                if progress:
                    progress(size, size)
                break
            last = f"HTTP {resp.status_code}: {resp.text[:200]}"
        if attempt < cfg["tiktok"]["max_retries"]:
            time.sleep(min(2 ** (attempt + 1), 60))
    else:
        raise TikTokError(f"TikTok не принял файл: {last}")
    mode = cfg["tiktok"]["mode"]
    link = links.get(cfg, user, "tiktok") or {}
    result = {"publish_id": publish_id, "mode": mode, "user": user,
              "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if link.get("username"):
        result["url"] = PROFILE_URL.format(name=link["username"])
    return result


def publish_status(cfg: dict, user: str, publish_id: str) -> dict:
    """Что стало с отправкой: PROCESSING_UPLOAD / PUBLISH_COMPLETE / FAILED."""
    return _api(STATUS_URL, access_token(cfg, user), {"publish_id": publish_id})
