"""Выгрузка роликов на площадки: привязки, resumable-загрузка YouTube, отправка в TikTok,
учётки панели и маршруты личного кабинета."""
import json

import pytest
import requests
from fastapi.testclient import TestClient

import panel.app
import upload
from panel import users
from pipeline import manifest
from pipeline.worker import open_queue
from upload import history, links, tiktok, youtube


@pytest.fixture
def cfg(dev_cfg, tmp_path, monkeypatch):
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    dev_cfg["paths"]["manifest"] = str(tmp_path / "out" / "manifest.json")
    dev_cfg["paths"]["users"] = str(tmp_path / "users.json")
    dev_cfg["paths"]["panel_secret"] = str(tmp_path / "panel-secret")
    dev_cfg["paths"]["social_links"] = str(tmp_path / "social-links.json")
    dev_cfg["paths"]["uploads"] = str(tmp_path / "out" / "uploads.json")
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["panel"]["password"] = None
    dev_cfg["panel"]["public_url"] = "https://panel.example"
    monkeypatch.setattr(panel.app, "start_worker", lambda cfg: False)
    monkeypatch.setattr(youtube, "credentials", lambda: ("client-id", "client-secret"))
    monkeypatch.setattr(tiktok, "credentials", lambda: ("client-key", "client-secret"))
    return dev_cfg


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.headers = headers or {}
        self.text = json.dumps(self._body, ensure_ascii=False)

    def json(self):
        if isinstance(self._body, Exception):
            raise ValueError("не JSON")
        return self._body


def video_file(cfg, size=3 * 2**20):
    path = tmp_video = (panel.app.output_dir(cfg, "videos", "imessage_drama_abc12345.mp4"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x00" * size)
    return tmp_video


def make_video(cfg, scene_id="11111111-2222-3333-4444-555555555555", size=3 * 2**20):
    path = video_file(cfg, size)
    manifest._save(manifest._manifest_path(cfg), [{
        "scene_id": scene_id, "type": "imessage_drama", "title": "Свадьба", "source": "deepseek", "url": "",
        "duration_s": 55.0, "output": f"output/videos/{path.name}",
        "created_at": "2026-09-17T10:00:00+00:00", "rendered_at": "2026-09-17T10:20:00+00:00"}])
    return scene_id, path


# --- привязки и история -------------------------------------------------------------

def test_links_saved_per_user_and_platform(cfg):
    links.save(cfg, "admin", "youtube", {"refresh_token": "r1", "channel_title": "Канал"})
    links.save(cfg, "user1", "tiktok", {"access_token": "t1"})
    assert links.get(cfg, "admin", "youtube")["channel_title"] == "Канал"
    assert links.get(cfg, "admin", "tiktok") is None
    assert links.users_with(cfg, "youtube") == ["admin"]
    assert set(links.of_user(cfg, "admin")) == {"youtube"}
    assert links.remove(cfg, "admin", "youtube") is True
    assert links.remove(cfg, "admin", "youtube") is False        # второй раз убирать нечего
    assert links.get(cfg, "user1", "tiktok")["access_token"] == "t1"


def test_history_records_and_forgets(cfg):
    history.record(cfg, "s1", "youtube", {"video_id": "abc"})
    history.record(cfg, "s1", "tiktok", {"publish_id": "p1"})
    assert set(history.of_scene(cfg, "s1")) == {"youtube", "tiktok"}
    history.forget(cfg, "s1")
    assert history.of_scene(cfg, "s1") == {}


# --- YouTube -------------------------------------------------------------------------

def test_video_body_trims_to_youtube_limits(cfg):
    body = youtube.video_body(cfg, "Заголовок <секрет> " + "я" * 200, "Описание\n\nс переносами " + "т" * 6000,
                              ["#тег", "#второй", "", "#" + "д" * 600])
    snippet, status = body["snippet"], body["status"]
    assert len(snippet["title"]) == youtube.TITLE_MAX and "<" not in snippet["title"]
    assert len(snippet["description"]) == youtube.DESCRIPTION_MAX
    assert snippet["tags"] == ["тег", "второй"]                  # пустой отброшен, слишком длинный не влез
    assert snippet["categoryId"] == "24"
    assert status["privacyStatus"] == "private"                  # без аудита Google иначе и не даст
    assert status["containsSyntheticMedia"] is True


def test_youtube_upload_resumes_after_server_error(cfg, monkeypatch):
    """Обрыв посреди загрузки не валит задачу: у YouTube спрашивается принятое и отправка идёт дальше."""
    monkeypatch.setattr(youtube, "CHUNK", 2**20)
    monkeypatch.setattr(youtube, "access_token", lambda cfg, user: "token")
    monkeypatch.setattr(youtube.time, "sleep", lambda s: None)
    _, path = make_video(cfg, size=3 * 2**20)
    calls = []

    def fake_post(url, **kw):
        assert kw["headers"]["X-Upload-Content-Length"] == str(3 * 2**20)
        return FakeResponse(200, {}, {"Location": "https://upload.example/session"})

    size = 3 * 2**20
    state = {"accepted": 0, "stumbled": False}

    def fake_put(url, headers=None, data=None, **kw):
        span = headers["Content-Range"]
        calls.append(span)
        if span == f"bytes */{size}":                                # запрос «сколько принято»
            return FakeResponse(308, {}, {"Range": f"bytes=0-{state['accepted'] - 1}"})
        start = int(span.split()[1].split("-")[0])
        if start == 2**20 and not state["stumbled"]:                 # второй кусок — сбой на стороне YouTube
            state["stumbled"] = True
            return FakeResponse(503, {"error": {"message": "backend"}})
        state["accepted"] = start + len(data)
        if state["accepted"] >= size:
            return FakeResponse(200, {"id": "vid123", "status": {"privacyStatus": "private"}})
        return FakeResponse(308, {}, {"Range": f"bytes=0-{state['accepted'] - 1}"})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "put", fake_put)
    seen = []
    result = youtube.upload(cfg, "admin", path, youtube.video_body(cfg, "Т", "О", []),
                            lambda done, total: seen.append((done, total)))
    assert result["video_id"] == "vid123" and result["privacy"] == "private"
    assert result["url"].endswith("vid123") and "studio.youtube.com" in result["studio_url"]
    assert f"bytes */{3 * 2**20}" in calls                           # после 503 спросили принятое
    assert seen                                                      # ход загрузки сообщался наружу


def test_youtube_upload_gives_up_with_reason(cfg, monkeypatch):
    monkeypatch.setattr(youtube, "access_token", lambda cfg, user: "token")
    _, path = make_video(cfg)
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(
        403, {"error": {"message": "quota", "errors": [{"reason": "quotaExceeded"}]}}))
    with pytest.raises(youtube.YouTubeError, match="quotaExceeded"):
        youtube.upload(cfg, "admin", path, {}, None)


def test_youtube_access_token_refreshed_and_saved(cfg, monkeypatch):
    links.save(cfg, "admin", "youtube", {"refresh_token": "r1", "access_token": "старый", "expires_at": 0})
    monkeypatch.setattr(youtube, "_token_request", lambda payload: {"access_token": "новый", "expires_in": 3600})
    assert youtube.access_token(cfg, "admin") == "новый"
    assert links.get(cfg, "admin", "youtube")["access_token"] == "новый"   # сохранён, второй раз не запрашиваем


def test_youtube_auth_url_has_redirect_and_offline(cfg):
    url = youtube.auth_url(cfg, "state-1")
    assert "https://panel.example/api/social/youtube/callback".replace("/", "%2F").split("%2F")[-1] in url
    assert "access_type=offline" in url and "prompt=consent" in url and "state=state-1" in url


# --- TikTok --------------------------------------------------------------------------

def test_tiktok_inbox_upload(cfg, monkeypatch):
    """Режим inbox: ролик уходит в черновики, подпись автор дописывает в приложении."""
    monkeypatch.setattr(tiktok, "access_token", lambda cfg, user: "token")
    links.save(cfg, "admin", "tiktok", {"access_token": "t", "username": "farm"})
    _, path = make_video(cfg)
    sent = {}

    def fake_post(url, json=None, **kw):
        sent["url"] = url
        sent["payload"] = json
        return FakeResponse(200, {"data": {"publish_id": "pub-1", "upload_url": "https://up.tiktok/x"},
                                  "error": {"code": "ok"}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "put", lambda url, **kw: FakeResponse(200))
    result = tiktok.upload(cfg, "admin", path, "подпись")
    assert sent["url"] == tiktok.INBOX_INIT_URL          # не прямая публикация
    assert sent["payload"]["source_info"]["total_chunk_count"] == 1
    assert result["publish_id"] == "pub-1" and result["mode"] == "inbox"
    assert result["url"].endswith("@farm")


def test_tiktok_direct_falls_back_to_allowed_privacy(cfg, monkeypatch):
    """Приложению без аудита TikTok отдаёт только SELF_ONLY — берём его, а не желаемое из конфига."""
    cfg["tiktok"]["mode"] = "direct"
    cfg["tiktok"]["privacy_level"] = "PUBLIC_TO_EVERYONE"
    monkeypatch.setattr(tiktok, "access_token", lambda cfg, user: "token")
    _, path = make_video(cfg)
    sent = {}

    def fake_post(url, json=None, **kw):
        if url == tiktok.CREATOR_URL:
            return FakeResponse(200, {"data": {"privacy_level_options": ["SELF_ONLY"]}, "error": {"code": "ok"}})
        sent["payload"] = json
        return FakeResponse(200, {"data": {"publish_id": "pub-2", "upload_url": "https://up.tiktok/y"},
                                  "error": {"code": "ok"}})

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "put", lambda url, **kw: FakeResponse(200))
    tiktok.upload(cfg, "admin", path, "подпись")
    assert sent["payload"]["post_info"]["privacy_level"] == "SELF_ONLY"
    assert sent["payload"]["post_info"]["title"] == "подпись"


def test_tiktok_refuses_too_big_file(cfg, monkeypatch):
    monkeypatch.setattr(tiktok, "SINGLE_CHUNK_MAX", 2**20)
    _, path = make_video(cfg, size=2 * 2**20)
    with pytest.raises(tiktok.TikTokError, match="64 МБ"):
        tiktok.upload(cfg, "admin", path, "подпись")


def test_tiktok_error_message_from_api(cfg, monkeypatch):
    monkeypatch.setattr(tiktok, "access_token", lambda cfg, user: "token")
    _, path = make_video(cfg)
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(
        200, {"error": {"code": "spam_risk_too_many_posts", "message": "слишком часто"}}))
    with pytest.raises(tiktok.TikTokError, match="слишком часто"):
        tiktok.upload(cfg, "admin", path, "подпись")


# --- общий путь выгрузки -------------------------------------------------------------

def test_upload_scene_uses_publish_texts_and_records(cfg, monkeypatch):
    from pipeline.publish import save_publish
    scene_id, path = make_video(cfg)
    save_publish(cfg, scene_id, {"tiktok": "подпись для тиктока", "shorts_title": "Заголовок Shorts",
                                 "shorts_description": "описание", "hashtags": ["#драма"]})
    links.save(cfg, "admin", "youtube", {"refresh_token": "r"})
    seen = {}

    def fake_upload(cfg_, user, video, body, progress=None):
        seen["title"] = body["snippet"]["title"]
        seen["tags"] = body["snippet"]["tags"]
        return {"video_id": "v1", "url": "https://youtu.be/v1", "privacy": "private"}

    monkeypatch.setattr(youtube, "upload", fake_upload)
    result = upload.upload_scene(cfg, scene_id, "youtube", "admin")
    assert seen["title"] == "Заголовок Shorts" and seen["tags"] == ["драма"]
    assert result["video_id"] == "v1"
    assert history.of_scene(cfg, scene_id)["youtube"]["video_id"] == "v1"


def test_upload_scene_without_link_or_video(cfg):
    scene_id, path = make_video(cfg)
    with pytest.raises(upload.UploadError, match="не привязан"):
        upload.upload_scene(cfg, scene_id, "youtube", "admin")
    links.save(cfg, "admin", "youtube", {"refresh_token": "r"})
    path.unlink()
    with pytest.raises(upload.UploadError, match="не найден"):
        upload.upload_scene(cfg, scene_id, "youtube", "admin")
    with pytest.raises(upload.UploadError, match="Нет такого ролика"):
        upload.upload_scene(cfg, "нет-такого", "youtube", "admin")


def test_upload_scene_falls_back_to_manifest_title(cfg, monkeypatch):
    """Тексты ещё не написаны — отправка не встаёт, берём название из манифеста."""
    scene_id, _ = make_video(cfg)
    links.save(cfg, "admin", "youtube", {"refresh_token": "r"})
    seen = {}
    monkeypatch.setattr(youtube, "upload", lambda c, u, v, body, p=None: seen.update(
        title=body["snippet"]["title"]) or {"video_id": "v2"})
    upload.upload_scene(cfg, scene_id, "youtube", "admin")
    assert seen["title"] == "Свадьба"


# --- панель ---------------------------------------------------------------------------

def client(cfg, login="admin"):
    app = TestClient(panel.app.create_app(cfg))
    password = cfg["panel"]["seed"][login]["password"]
    assert app.post("/api/login", json={"login": login, "password": password},
                    headers={"X-Panel": "1"}).status_code == 200
    return app


def test_me_shows_platform_state(cfg):
    app = client(cfg)
    me = app.get("/api/me").json()
    assert me["platforms"]["youtube"]["configured"] is True
    assert me["platforms"]["youtube"]["linked"] is False
    assert me["platforms"]["youtube"]["redirect_uri"] == "https://panel.example/api/social/youtube/callback"
    assert me["youtube_privacy"] == "private" and me["tiktok_mode"] == "inbox"
    links.save(cfg, "admin", "youtube", {"refresh_token": "r", "channel_title": "Мой канал"})
    assert app.get("/api/me").json()["platforms"]["youtube"]["account"] == "Мой канал"


def test_link_callback_checks_state_and_owner(cfg, monkeypatch):
    app = client(cfg)
    url = app.get("/api/social/youtube/link").json()["url"]
    assert url.startswith(youtube.AUTH_URL)
    state = url.split("state=")[1].split("&")[0]
    monkeypatch.setattr(youtube, "link_account",
                        lambda c, user, code: {"channel_title": f"канал {user}"})
    # чужой state не принимается
    assert "не найдена" in app.get("/api/social/youtube/callback",
                                   params={"code": "c", "state": "подделка"}).text
    ok = app.get("/api/social/youtube/callback", params={"code": "c", "state": state})
    assert "канал admin" in ok.text
    # тот же state второй раз уже не сработает — запись одноразовая
    assert "не найдена" in app.get("/api/social/youtube/callback",
                                   params={"code": "c", "state": state}).text


def test_unlink_removes_only_own_link(cfg, monkeypatch):
    links.save(cfg, "admin", "youtube", {"refresh_token": "r"})
    links.save(cfg, "user1", "youtube", {"refresh_token": "r2"})
    monkeypatch.setattr(requests, "post", lambda url, **kw: FakeResponse(200))
    assert client(cfg).delete("/api/social/youtube", headers={"X-Panel": "1"}).json() == {"unlinked": True}
    assert links.get(cfg, "admin", "youtube") is None
    assert links.get(cfg, "user1", "youtube") is not None


def test_upload_route_queues_job_once(cfg):
    scene_id, _ = make_video(cfg)
    app = client(cfg)
    x = {"X-Panel": "1"}
    body = {"platform": "youtube"}
    # без привязки отправлять некуда
    assert app.post(f"/api/videos/{scene_id}/upload", json=body, headers=x).status_code == 400
    links.save(cfg, "admin", "youtube", {"refresh_token": "r"})
    first = app.post(f"/api/videos/{scene_id}/upload", json=body, headers=x).json()
    assert first["new"] is True
    again = app.post(f"/api/videos/{scene_id}/upload", json=body, headers=x).json()
    assert again["new"] is False and again["job"] == first["job"]      # второе нажатие не выложит дважды
    queue = open_queue(cfg)
    try:
        jobs = queue.jobs("pending", kinds=("social_upload",))
    finally:
        queue.close()
    assert [j.payload for j in jobs] == [{"scene_id": scene_id, "platform": "youtube", "user": "admin"}]
    assert app.post(f"/api/videos/{scene_id}/upload", json={"platform": "вконтакте"},
                    headers=x).status_code == 404


def test_uploaded_state_shown_in_videos(cfg):
    scene_id, _ = make_video(cfg)
    history.record(cfg, scene_id, "youtube", {"video_id": "v1", "privacy": "private",
                                              "studio_url": "https://studio.youtube.com/video/v1/edit"})
    videos = client(cfg).get("/api/videos").json()
    assert videos[0]["uploads"]["youtube"]["video_id"] == "v1"


def test_deleting_video_forgets_uploads(cfg):
    scene_id, _ = make_video(cfg)
    history.record(cfg, scene_id, "youtube", {"video_id": "v1"})
    app = client(cfg)
    assert app.delete(f"/api/videos/{scene_id}", headers={"X-Panel": "1"}).status_code == 200
    assert history.of_scene(cfg, scene_id) == {}


def test_seed_users_created_once_and_password_kept(cfg):
    users.ensure_seed(cfg)
    assert sorted(u["login"] for u in users.list_users(cfg)) == ["admin", "user1", "user2", "user3",
                                                                "user4", "user5"]
    users.set_password(cfg, "user1", "свой-пароль")
    assert users.ensure_seed(cfg) == []                       # повторный запуск ничего не создаёт
    assert users.authenticate(cfg, "user1", "свой-пароль") is not None
    assert users.authenticate(cfg, "user1", "user167") is None    # заводской не вернулся


def test_password_hashes_are_salted(cfg):
    one, two = users.hash_password("одинаковый"), users.hash_password("одинаковый")
    assert one != two and "одинаковый" not in one             # соль у каждой записи своя
    assert users.verify_password("одинаковый", one) and not users.verify_password("другой", one)


def test_session_token_rejects_tampering(cfg):
    users.ensure_seed(cfg)
    token = users.make_token(cfg, "admin")
    assert users.read_token(cfg, token) == "admin"
    assert users.read_token(cfg, token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert users.read_token(cfg, "мусор") is None
    users.delete_user(cfg, "user1")
    assert users.read_token(cfg, users.make_token(cfg, "user1")) is None   # учётки нет — кука недействительна
