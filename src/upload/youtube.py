"""YouTube: привязка канала и загрузка готовых роликов (Data API v3, OAuth 2.0).

Ключи приложения — в .env (YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET; Google Cloud Console,
тип клиента «Web application», разрешённый redirect — `<адрес панели>/api/social/youtube/callback`).
Канал привязывается в личном кабинете панели, токены — `data/social-links.json` (upload.links).

Важно: ролики, загруженные через API-проект без аудита Google (все проекты, созданные после
28.07.2020), принудительно становятся private, и снять это может только аудит. Поэтому
youtube.privacy_status по умолчанию private: ролик уезжает на канал с готовыми заголовком,
описанием и тегами, а владелец открывает его одним нажатием в YouTube Studio.

Без сторонних библиотек (как notify/telegram.py): requests уже в зависимостях.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
from pathlib import Path

import requests

from core import config
from upload import links

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
WATCH_URL = "https://www.youtube.com/watch?v={id}"
STUDIO_URL = "https://studio.youtube.com/video/{id}/edit"
SCOPES = ("https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube.readonly")

CHUNK = 8 * 2**20          # кратен 256 КиБ, как требует протокол resumable-загрузки
RETRY_STATUS = (429, 500, 502, 503, 504)
TITLE_MAX, DESCRIPTION_MAX, TAGS_MAX = 100, 5000, 450


class YouTubeError(RuntimeError):
    pass


def credentials() -> tuple[str | None, str | None]:
    """client_id и client_secret из .env. Скобки вокруг значения («<id>») отбрасываются — как в notify."""
    env = config.read_env_file()
    value = lambda name: (os.environ.get(name) or env.get(name) or "").strip().strip("<>").strip() or None  # noqa: E731
    return value("YOUTUBE_CLIENT_ID"), value("YOUTUBE_CLIENT_SECRET")


def configured() -> bool:
    return all(credentials())


# --- привязка канала ------------------------------------------------------------

def redirect_uri(cfg: dict) -> str:
    return config.public_url(cfg).rstrip("/") + "/api/social/youtube/callback"


def auth_url(cfg: dict, state: str) -> str:
    """Ссылка на согласие Google. access_type=offline и prompt=consent — иначе refresh-токен
    не выдаётся и привязка проживёт час."""
    client_id, _ = credentials()
    if not client_id:
        raise YouTubeError("В .env нет YOUTUBE_CLIENT_ID и YOUTUBE_CLIENT_SECRET")
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri(cfg), "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
        "include_granted_scopes": "true"})
    return f"{AUTH_URL}?{query}"


def _token_request(payload: dict) -> dict:
    try:
        resp = requests.post(TOKEN_URL, data=payload, timeout=30)
        data = resp.json()
    except (requests.RequestException, ValueError) as e:
        raise YouTubeError(f"Google не ответил на запрос токена: {e}") from e
    if resp.status_code != 200:
        # в текст ошибки идёт только описание от Google: client_secret остаётся в payload
        raise YouTubeError(f"Google отказал в токене: {data.get('error_description') or data.get('error') or data}")
    return data


def _channel(token: str) -> dict:
    try:
        resp = requests.get(CHANNELS_URL, params={"part": "snippet", "mine": "true"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=30)
        items = resp.json().get("items") or []
    except (requests.RequestException, ValueError):
        return {}
    if not items:
        return {}
    return {"channel_id": items[0].get("id", ""),
            "channel_title": (items[0].get("snippet") or {}).get("title", "")}


def link_account(cfg: dict, user: str, code: str) -> dict:
    """Код согласия → refresh-токен, имя канала; привязка сохраняется за пользователем панели."""
    client_id, client_secret = credentials()
    data = _token_request({"client_id": client_id, "client_secret": client_secret, "code": code,
                           "grant_type": "authorization_code", "redirect_uri": redirect_uri(cfg)})
    if not data.get("refresh_token"):
        raise YouTubeError("Google не выдал refresh-токен — отзовите доступ приложению "
                           "в аккаунте Google и привяжите канал заново")
    link = {"refresh_token": data["refresh_token"], "access_token": data.get("access_token", ""),
            "expires_at": time.time() + int(data.get("expires_in", 3600)),
            "linked_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            **_channel(data.get("access_token", ""))}
    links.save(cfg, user, "youtube", link)
    return link


def unlink_account(cfg: dict, user: str) -> bool:
    """Отвязать канал: отзываем токен у Google (тихо — привязку убираем в любом случае)."""
    link = links.get(cfg, user, "youtube")
    if link:
        try:
            requests.post(REVOKE_URL, data={"token": link.get("refresh_token", "")}, timeout=15)
        except requests.RequestException as e:
            log.warning("YouTube: отозвать токен не удалось (%s) — привязка убрана только у нас", e)
    return links.remove(cfg, user, "youtube")


def access_token(cfg: dict, user: str) -> str:
    """Действующий access-токен пользователя: обновляется по refresh-токену (живёт около часа)."""
    client_id, client_secret = credentials()
    if not (client_id and client_secret):
        raise YouTubeError("В .env нет YOUTUBE_CLIENT_ID и YOUTUBE_CLIENT_SECRET")
    link = links.get(cfg, user, "youtube")
    if not link or not link.get("refresh_token"):
        raise YouTubeError("Канал YouTube не привязан — личный кабинет → «Привязать YouTube»")
    if link.get("access_token") and link.get("expires_at", 0) > time.time() + 60:
        return link["access_token"]
    fresh = _token_request({"client_id": client_id, "client_secret": client_secret,
                            "refresh_token": link["refresh_token"], "grant_type": "refresh_token"})
    link["access_token"] = fresh["access_token"]
    link["expires_at"] = time.time() + int(fresh.get("expires_in", 3600))
    links.save(cfg, user, "youtube", link)
    return link["access_token"]


# --- загрузка ролика ------------------------------------------------------------

def video_body(cfg: dict, title: str, description: str, tags: list[str]) -> dict:
    """Тело запроса videos.insert. Пределы YouTube: заголовок 100 знаков, описание 5000,
    теги — 500 знаков суммарно; угловые скобки в заголовке и описании запрещены."""
    yt = cfg["youtube"]
    clean = lambda s: " ".join(str(s or "").replace("<", "(").replace(">", ")").split())  # noqa: E731
    kept, total = [], 0
    for tag in tags:
        tag = clean(tag).lstrip("#")
        if tag and total + len(tag) + 1 <= TAGS_MAX:
            kept.append(tag)
            total += len(tag) + 1
    description = clean(description)[:DESCRIPTION_MAX]
    return {
        "snippet": {"title": clean(title)[:TITLE_MAX] or "Shorts", "description": description,
                    "tags": kept, "categoryId": str(yt["category_id"]), "defaultLanguage": yt["language"]},
        "status": {"privacyStatus": yt["privacy_status"], "selfDeclaredMadeForKids": bool(yt["made_for_kids"]),
                   # ролик собран из синтезированной речи — YouTube требует это отмечать
                   "containsSyntheticMedia": bool(yt["synthetic_media"])},
    }


def _start_session(token: str, body: dict, size: int) -> str:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
               "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"}
    try:
        resp = requests.post(UPLOAD_URL, params={"uploadType": "resumable", "part": "snippet,status"},
                             headers=headers, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                             timeout=60)
    except requests.RequestException as e:
        raise YouTubeError(f"YouTube не ответил на начало загрузки: {e}") from e
    if resp.status_code != 200 or not resp.headers.get("Location"):
        raise YouTubeError(f"YouTube отказал в загрузке: {_error_text(resp)}")
    return resp.headers["Location"]


def _error_text(resp: requests.Response) -> str:
    try:
        error = resp.json().get("error") or {}
    except ValueError:
        return f"HTTP {resp.status_code}: {resp.text[:200]}"
    reason = (error.get("errors") or [{}])[0].get("reason", "")
    message = error.get("message", resp.text[:200])
    return f"HTTP {resp.status_code} {reason}: {message}".strip()


def _uploaded_bytes(resp: requests.Response) -> int:
    """Сколько байт YouTube уже принял (заголовок Range у ответа 308)."""
    got = resp.headers.get("Range", "")
    return int(got.rsplit("-", 1)[-1]) + 1 if got.startswith("bytes=0-") else 0


def upload(cfg: dict, user: str, video: Path, body: dict, progress=None) -> dict:
    """Загрузка ролика по частям с продолжением после обрыва (resumable-протокол).

    Обрыв сети или 5xx не валит задачу: у YouTube спрашивается, сколько байт он принял, и
    отправка продолжается с этого места. progress(отправлено, всего) — для лога и панели."""
    video = Path(video)
    if not video.is_file():
        raise YouTubeError(f"Нет файла ролика: {video}")
    size = video.stat().st_size
    token = access_token(cfg, user)
    session = _start_session(token, body, size)
    log.info("YouTube: загрузка %s (%.1f МБ) от имени %s", video.name, size / 2**20, user)
    sent, attempt = 0, 0
    with video.open("rb") as fh:
        while sent < size:
            fh.seek(sent)
            chunk = fh.read(CHUNK)
            headers = {"Authorization": f"Bearer {token}", "Content-Type": "video/mp4",
                       "Content-Range": f"bytes {sent}-{sent + len(chunk) - 1}/{size}"}
            try:
                resp = requests.put(session, headers=headers, data=chunk, timeout=300)
            except requests.RequestException as e:
                attempt += 1
                if attempt > cfg["youtube"]["max_retries"]:
                    raise YouTubeError(f"YouTube оборвал загрузку: {e}") from e
                sent = _resume_from(session, token, size, sent, attempt)
                continue
            if resp.status_code in (200, 201):
                data = resp.json()
                video_id = data.get("id", "")
                log.info("YouTube: готово — %s", WATCH_URL.format(id=video_id))
                return {"video_id": video_id, "url": WATCH_URL.format(id=video_id),
                        "studio_url": STUDIO_URL.format(id=video_id),
                        "privacy": (data.get("status") or {}).get("privacyStatus", ""),
                        "uploaded_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "user": user}
            if resp.status_code == 308:
                accepted = _uploaded_bytes(resp) or sent + len(chunk)
                if accepted <= sent:
                    # YouTube принял кусок, но счётчик не сдвинулся — иначе слали бы одно и то же вечно
                    attempt += 1
                    if attempt > cfg["youtube"]["max_retries"]:
                        raise YouTubeError(f"YouTube не продвигается дальше {sent} из {size} байт")
                    time.sleep(min(2 ** attempt, 60))
                    continue
                sent, attempt = accepted, 0
                if progress:
                    progress(sent, size)
                continue
            if resp.status_code in RETRY_STATUS:
                attempt += 1
                if attempt > cfg["youtube"]["max_retries"]:
                    raise YouTubeError(f"YouTube не принял ролик: {_error_text(resp)}")
                sent = _resume_from(session, token, size, sent, attempt)
                continue
            if resp.status_code == 401:               # токен истёк посреди долгой загрузки
                attempt += 1
                if attempt > cfg["youtube"]["max_retries"]:
                    raise YouTubeError(f"YouTube не принял ролик: {_error_text(resp)}")
                token = access_token(cfg, user)
                continue
            raise YouTubeError(f"YouTube не принял ролик: {_error_text(resp)}")
    raise YouTubeError("YouTube принял все байты, но не вернул ролик")


def _resume_from(session: str, token: str, size: int, sent: int, attempt: int) -> int:
    """Спросить у YouTube, сколько байт принято, и подождать перед новой попыткой.
    Не ответил — шлём с того же места: лишний кусок он отбросит сам."""
    time.sleep(min(2 ** attempt, 60))
    try:
        resp = requests.put(session, headers={"Authorization": f"Bearer {token}",
                                              "Content-Range": f"bytes */{size}"}, timeout=60)
    except requests.RequestException:
        return sent
    if resp.status_code in (200, 201):
        return size
    return _uploaded_bytes(resp) or sent
