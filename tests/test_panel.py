"""Отбор сценариев, веб-панель, разнообразие оформления субтитров, автоочистка."""
import base64
import uuid

import pytest
from fastapi.testclient import TestClient

import panel.app
import pipeline.jobs
from core.scene import load_scene, save_scene, scene_path, work_dir
from pipeline import manifest
from pipeline.review import (ReviewError, approve, auto_approve, list_scenes, mark_pending, processing_chain, reject,
                             restore, set_auto_approve, set_texts)
from pipeline.worker import open_queue
from subtitles.scene import pick_style


@pytest.fixture
def cfg(dev_cfg, tmp_path, monkeypatch):
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    dev_cfg["paths"]["manifest"] = str(tmp_path / "out" / "manifest.json")
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["panel"]["password"] = None
    # учётки, ключ подписи сессий и привязки площадок — во временной папке, а не в data/ репозитория
    dev_cfg["paths"]["users"] = str(tmp_path / "users.json")
    dev_cfg["paths"]["panel_secret"] = str(tmp_path / "panel-secret")
    dev_cfg["paths"]["social_links"] = str(tmp_path / "social-links.json")
    dev_cfg["paths"]["uploads"] = str(tmp_path / "out" / "uploads.json")
    monkeypatch.setattr(panel.app, "start_worker", lambda cfg: False)   # без реального фонового процесса
    return dev_cfg


def panel_client(cfg, login="admin"):
    """Клиент панели с выполненным входом: без сессии все /api отвечают 401."""
    client = TestClient(panel.app.create_app(cfg))
    password = cfg["panel"]["seed"][login]["password"]
    resp = client.post("/api/login", json={"login": login, "password": password}, headers={"X-Panel": "1"})
    assert resp.status_code == 200, resp.text
    return client


def make_scene(cfg, kind="reddit_story", status="pending", created="2026-09-14T10:00:00Z"):
    sid = str(uuid.uuid4())
    if kind == "imessage_drama":
        script = [{"speaker": "A", "text": "Ты правда думала, что я не узнаю?"}, {"speaker": "B", "text": "О чём ты?"}]
        meta = {"title": "Скрин", "contacts": {"A": "Лена", "B": "Я"}}
    else:
        script = [{"speaker": "narrator", "text": "Три года сосед занимал моё место."},
                  {"speaker": "narrator", "text": "Я повесил табличку."}]
        meta = {"title": "Табличка", "source": {"source": "reddit", "url": "https://reddit.com/x"}}
    scene = {"scene_id": sid, "type": kind, "created_at": created, "script": script, "meta": meta,
             "output": {"resolution": "1080x1920", "path": f"output/videos/{sid}.mp4"}}
    mark_pending(scene)
    scene["meta"]["review"]["status"] = status
    save_scene(scene, scene_path(cfg, sid))
    return scene


# --- отбор -----------------------------------------------------------------------

def test_processing_chain(cfg, tmp_path):
    kinds = lambda scene: [j["kind"] for j in processing_chain(scene, cfg, tmp_path / "s.json")]  # noqa: E731
    assert kinds({"type": "reddit_story"}) == ["tts_synthesis", "subtitle_generation", "video_render"]
    assert kinds({"type": "imessage_drama"}) == ["tts_synthesis", "ui_render", "video_render"]


def test_list_scenes_by_status(cfg):
    old = make_scene(cfg, created="2026-09-14T09:00:00Z")
    new = make_scene(cfg, kind="imessage_drama", created="2026-09-14T11:00:00Z")
    make_scene(cfg, status="rejected")
    assert [s["scene_id"] for s in list_scenes(cfg, "pending")] == [new["scene_id"], old["scene_id"]]
    assert len(list_scenes(cfg, "rejected")) == 1 and len(list_scenes(cfg)) == 3


def test_edit_approve_reject_restore(cfg):
    scene = make_scene(cfg)
    sid = scene["scene_id"]
    edited = set_texts(cfg, sid, ["Четыре года сосед занимал моё место.", "   "])
    assert [l["text"] for l in edited["script"]] == ["Четыре года сосед занимал моё место."]   # пустая — удалена
    with pytest.raises(ReviewError, match="Ожидалось"):
        set_texts(cfg, sid, ["раз", "два"])

    queue = open_queue(cfg)
    ids = approve(cfg, sid, queue)
    assert [queue.get(i).kind for i in ids] == ["tts_synthesis", "subtitle_generation", "video_render"]
    assert queue.get(ids[1]).depends_on == ids[0]
    with pytest.raises(ReviewError, match="approved"):
        approve(cfg, sid, queue)
    queue.close()
    assert load_scene(scene_path(cfg, sid))["meta"]["review"]["status"] == "approved"

    other = make_scene(cfg)["scene_id"]
    edited = set_texts(cfg, other, ["Раз.", "Два."], ["anger", "neutral"])
    assert [l["emotion"] for l in edited["script"]] == ["anger", "neutral"]
    assert set_texts(cfg, other, ["Раз.", "Два!"])["script"][0]["emotion"] == "anger"   # без emotions — как было
    with pytest.raises(ReviewError, match="эмоция"):
        set_texts(cfg, other, ["Раз.", "Два."], ["anger", "ярость"])
    with pytest.raises(ReviewError, match="эмоций"):
        set_texts(cfg, other, ["Раз.", "Два."], ["anger"])
    reject(cfg, other)
    with pytest.raises(ReviewError):
        set_texts(cfg, other, ["x", "y"])                                    # отклонённый не правится
    assert restore(cfg, other)["meta"]["review"]["status"] == "pending"


def test_script_waits_for_review(cfg, tmp_path):
    scene = {"scene_id": str(uuid.uuid4()), "type": "reddit_story", "created_at": "2026-09-14T10:00:00Z",
             "script": [{"speaker": "narrator", "text": "Текст."}], "meta": {"title": "Т"},
             "output": {"resolution": "1080x1920", "path": "output/videos/x.mp4"}}
    path = scene_path(cfg, scene["scene_id"])
    result = pipeline.jobs._after_script(dict(scene), path, cfg)
    assert result["review"] == "pending" and "next" not in result
    assert load_scene(path)["meta"]["review"]["status"] == "pending"

    cfg["review"]["enabled"] = False
    result = pipeline.jobs._after_script(dict(scene), path, cfg)
    assert [j["kind"] for j in result["next"][0]] == ["tts_synthesis", "subtitle_generation", "video_render"]


def test_auto_approve_sends_script_to_work(cfg):
    scene = {"scene_id": str(uuid.uuid4()), "type": "imessage_drama", "created_at": "2026-09-15T10:00:00Z",
             "script": [{"speaker": "A", "text": "Привет."}], "meta": {"title": "Т"},
             "output": {"resolution": "1080x1920", "path": "output/videos/x.mp4"}}
    path = scene_path(cfg, scene["scene_id"])
    assert auto_approve(cfg) is False                                        # по умолчанию — ручная проверка
    set_auto_approve(cfg, True)
    result = pipeline.jobs._after_script(dict(scene), path, cfg)
    assert [j["kind"] for j in result["next"][0]] == ["tts_synthesis", "ui_render", "video_render"]
    review = load_scene(path)["meta"]["review"]
    assert review["status"] == "approved" and review["auto"] is True
    assert list_scenes(cfg, "pending") == []

    set_auto_approve(cfg, False)
    assert pipeline.jobs._after_script(dict(scene), path, cfg)["review"] == "pending"


def test_api_auto_approve_toggle(cfg):
    client = panel_client(cfg)
    summary = client.get("/api/summary").json()
    assert summary["auto_approve"] is False and summary["review_enabled"] is True
    assert client.put("/api/settings", json={"auto_approve": True}).status_code == 403            # без X-Panel
    r = client.put("/api/settings", json={"auto_approve": True}, headers={"X-Panel": "1"})
    assert r.status_code == 200 and r.json() == {"auto_approve": True}
    assert client.get("/api/summary").json()["auto_approve"] is True
    assert auto_approve(cfg) is True                                         # задачи сценариев видят то же
    assert client.put("/api/settings", json={"auto_approve": "да"}, headers={"X-Panel": "1"}).status_code == 422


def test_api_delete_scenes_in_bulk(cfg):
    client = panel_client(cfg)
    seen = [make_scene(cfg)["scene_id"] for _ in range(2)]
    fresh = make_scene(cfg)["scene_id"]                                      # появился после загрузки страницы
    rejected = [make_scene(cfg, status="rejected")["scene_id"] for _ in range(2)]
    approved = make_scene(cfg, status="approved")["scene_id"]
    x = {"X-Panel": "1"}

    assert client.delete("/api/scenes", params={"status": "pending", "ids": seen}).status_code == 403
    r = client.delete("/api/scenes", params={"status": "pending", "ids": seen}, headers=x)
    assert r.json() == {"deleted": 2}
    assert [s["scene_id"] for s in list_scenes(cfg, "pending")] == [fresh]  # невиденный — остался
    assert client.delete("/api/scenes", params={"status": "rejected"}, headers=x).json() == {"deleted": 2}
    assert not any(scene_path(cfg, i).exists() for i in seen + rejected)
    assert client.delete("/api/scenes", params={"status": "approved"}, headers=x).status_code == 400
    assert scene_path(cfg, approved).exists()                                # одобренные не удаляются


def test_api_delete_all_videos_only_listed(cfg):
    client = panel_client(cfg)
    videos = work_dir(cfg, {"scene_id": "x"}).parents[1] / "videos"
    videos.mkdir(parents=True)
    scenes = [make_scene(cfg, status="approved") for _ in range(3)]
    for scene in scenes:
        (videos / f"{scene['scene_id']}.mp4").write_bytes(b"\x00")
        manifest.record(cfg, scene, {"output": f"output/videos/{scene['scene_id']}.mp4", "duration_s": 50.0})
    listed = [s["scene_id"] for s in scenes[:2]]
    r = client.delete("/api/videos", params={"ids": listed}, headers={"X-Panel": "1"})
    assert r.json() == {"deleted": 2, "busy": 0}
    assert [v["scene_id"] for v in client.get("/api/videos").json()] == [scenes[2]["scene_id"]]
    assert sorted(p.name for p in videos.iterdir()) == [f"{scenes[2]['scene_id']}.mp4"]


def test_api_videos_show_time_after_approval(cfg):
    from datetime import datetime, timedelta, timezone
    client = panel_client(cfg)
    scene = make_scene(cfg, kind="imessage_drama", status="approved")
    sid = scene["scene_id"]
    approved_at = (datetime.now(timezone.utc) - timedelta(minutes=25)).isoformat(timespec="seconds")
    scene["meta"]["review"]["decided_at"] = approved_at
    save_scene(scene, scene_path(cfg, sid))
    manifest.record(cfg, scene, {"output": "output/videos/v.mp4", "duration_s": 55.0})

    queue = open_queue(cfg)
    payload = {"scene": f"output/scenes/{sid}.json"}
    spent = {"tts_synthesis": 120, "ui_render": 40, "video_render": 12}
    for kind, seconds in spent.items():
        job = queue.enqueue(kind, payload)
        queue.conn.execute("UPDATE jobs SET status='done', started_at=?, finished_at=? WHERE id=?",
                           ("2026-09-15T13:00:00", f"2026-09-15T13:{seconds // 60:02d}:{seconds % 60:02d}", job))
    queue.enqueue("tts_synthesis", {"scene": "output/scenes/other.json"})    # чужая и не выполненная
    queue.close()

    video = client.get("/api/videos").json()[0]
    assert video["approved_at"] == approved_at
    assert 25 * 60 - 5 <= video["elapsed_s"] <= 25 * 60 + 5                 # от одобрения до рендера
    assert video["work_s"] == 172                                           # озвучка + интерфейс + рендер

    clip = {"scene_id": str(uuid.uuid4()), "type": "video_clip", "created_at": "2026-09-15T10:00:00Z"}
    manifest.record(cfg, clip, {"output": "output/videos/c.mp4", "duration_s": 30.0})
    first = client.get("/api/videos").json()[0]
    assert first["approved_at"] is None and first["elapsed_s"] is None      # у клипов отбора нет


def test_api_delete_video(cfg):
    client = panel_client(cfg)
    scene = make_scene(cfg, status="approved")
    sid = scene["scene_id"]
    videos = work_dir(cfg, scene).parents[1] / "videos"
    videos.mkdir(parents=True)
    video = videos / f"reddit_story_{sid[:8]}.mp4"
    video.write_bytes(b"\x00" * 100)
    work = work_dir(cfg, scene)
    work.mkdir(parents=True)
    (work / "voice.wav").write_bytes(b"\x00" * 100)
    other = make_scene(cfg, status="approved")
    manifest.record(cfg, scene, {"output": f"output/videos/{video.name}", "duration_s": 55.0})
    manifest.record(cfg, other, {"output": "output/videos/gone.mp4", "duration_s": 50.0})
    assert len(client.get("/api/videos").json()) == 2

    assert client.delete(f"/api/videos/{sid}").status_code == 403                                 # без X-Panel
    r = client.delete(f"/api/videos/{sid}", headers={"X-Panel": "1"})
    assert r.status_code == 200 and r.json() == {"deleted": video.name}
    assert not video.exists() and not work.exists()
    assert [v["scene_id"] for v in client.get("/api/videos").json()] == [other["scene_id"]]
    assert scene_path(cfg, sid).exists()                                     # сценарий остаётся
    assert client.delete(f"/api/videos/{sid}", headers={"X-Panel": "1"}).status_code == 404
    assert client.delete("/api/videos/..%2F..%2Fx", headers={"X-Panel": "1"}).status_code == 404
    # запись без файла (файл удалили руками) тоже убирается
    assert client.delete(f"/api/videos/{other['scene_id']}", headers={"X-Panel": "1"}).status_code == 200
    assert client.get("/api/videos").json() == []


# --- API панели ---------------------------------------------------------------------

def test_api_review_flow(cfg):
    client = panel_client(cfg)
    scene = make_scene(cfg, kind="imessage_drama")
    cards = client.get("/api/scenes?status=pending").json()
    assert cards[0]["contacts"]["A"] == "Лена" and cards[0]["words"] == 10 and cards[0]["est_s"] > 0
    assert [l["emotion"] for l in cards[0]["script"]] == ["neutral", "neutral"]   # старая сцена без эмоций
    emotions = client.get("/api/summary").json()["emotions"]
    assert emotions[0] == {"id": "neutral", "label": "нейтрально"} and len(emotions) == 10

    url = f"/api/scenes/{scene['scene_id']}/approve"
    body = {"texts": ["Ты думала, я не узнаю?", "О чём ты?"], "emotions": ["sarcasm", "shock"]}
    assert client.post(url, json=body).status_code == 403                                           # без X-Panel
    bad = client.post(url, json={**body, "emotions": ["sarcasm", "ярость"]}, headers={"X-Panel": "1"})
    assert bad.status_code == 400 and "ярость" in bad.json()["detail"]
    r = client.post(url, json=body, headers={"X-Panel": "1"})
    assert r.status_code == 200 and len(r.json()["jobs"]) == 3
    saved = load_scene(scene_path(cfg, scene["scene_id"]))["script"]
    assert saved[0]["text"] == "Ты думала, я не узнаю?"
    assert [l["emotion"] for l in saved] == ["sarcasm", "shock"]
    assert client.post(url, headers={"X-Panel": "1"}).status_code == 400                           # уже одобрен
    assert client.get("/api/summary").json()["review"]["approved"] == 1


def test_api_generate_clamps_count(cfg):
    client = panel_client(cfg)
    r = client.post("/api/generate", json={"kind": "story", "count": 100, "topic": " соседи "},
                    headers={"X-Panel": "1"})
    queue = open_queue(cfg)
    jobs = [queue.get(i) for i in r.json()["jobs"]]
    assert len(jobs) == 20                                                   # не больше 20 за раз
    assert {j.kind for j in jobs} == {"script_story"} and jobs[0].payload == {"topic": "соседи"}
    queue.close()
    r = client.post("/api/generate", json={"kind": "drama", "count": 2, "topic": "  "}, headers={"X-Panel": "1"})
    assert len(r.json()["jobs"]) == 2
    assert client.post("/api/generate", json={"kind": "x"}, headers={"X-Panel": "1"}).status_code == 400


class SlowDeepSeek:
    """Подставной DeepSeek для переписывания по правке: отвечает, только когда тест разрешит."""

    def __init__(self, *answers, error=None):
        import threading
        self.answers, self.error, self.go, self.calls = list(answers), error, threading.Event(), []

    def __call__(self, cfg):
        return self

    def chat_json(self, system, user, temperature=None):
        self.calls.append(system)
        assert self.go.wait(5)
        if self.error:
            raise self.error
        return self.answers.pop(0)


def wait_revised(client, scene_id):
    import time
    for _ in range(100):
        card = client.get(f"/api/scenes/{scene_id}").json()
        if not card["revising"]:
            return card
        time.sleep(0.05)
    raise AssertionError("переписывание не закончилось")


def test_revise_with_note_in_background(cfg, monkeypatch):
    import scripting
    client = panel_client(cfg)
    x = {"X-Panel": "1"}
    scene = make_scene(cfg, kind="imessage_drama")
    sid = scene["scene_id"]
    revised = {"title": "Скрин", "contacts": {"A": "Лена", "B": "Я"},
               "messages": [{"speaker": "A", "emotion": "anger", "text": "Кто слил скрин?"},
                            {"speaker": "B", "text": "Твоя мама."}]}
    fake = SlowDeepSeek(revised, {"hook": 8, "twists": 7, "ending": 9, "language": 8, "total": 8,
                                  "verdict": "Жёстче.", "fix": ""})
    monkeypatch.setattr(scripting, "DeepSeekClient", fake)

    url = f"/api/scenes/{sid}/revise"
    assert client.post(url, json={"note": "  "}, headers=x).status_code == 400           # что изменить — обязательно
    body = {"note": "концовку жёстче", "texts": ["Ты думала, я не узнаю?", "О чём ты?"], "emotions": ["sarcasm", "shock"]}
    assert client.post(url, json=body).status_code == 403                                # без X-Panel
    r = client.post(url, json=body, headers=x)
    assert r.status_code == 202
    card = client.get(f"/api/scenes/{sid}").json()
    assert card["revising"] is True and card["script"][0]["text"] == "Ты думала, я не узнаю?"   # правки — до переписывания
    assert client.post(f"/api/scenes/{sid}/approve", headers=x).status_code == 400        # пока переписывается — нельзя
    assert client.post(url, json=body, headers=x).status_code == 400                     # и второй раз тоже

    fake.go.set()
    card = wait_revised(client, sid)
    assert [l["text"] for l in card["script"]] == ["Кто слил скрин?", "Твоя мама."]
    assert card["review"]["status"] == "pending" and card["review"]["revised_at"] and card["score"]["total"] == 8
    assert "по замечаниям:\nконцовку жёстче" in fake.calls[0] and "строгий редактор" in fake.calls[1]
    assert client.post(f"/api/scenes/{sid}/approve", headers=x).status_code == 200       # дальше — как обычно
    assert client.get("/api/scenes/нет-такого").status_code == 404


def test_revise_error_keeps_script(cfg, monkeypatch):
    import scripting
    from scripting import DeepSeekError
    client = panel_client(cfg)
    scene = make_scene(cfg)
    fake = SlowDeepSeek(error=DeepSeekError("Нет связи с DeepSeek 300 с"))
    fake.go.set()
    monkeypatch.setattr(scripting, "DeepSeekClient", fake)
    r = client.post(f"/api/scenes/{scene['scene_id']}/revise", json={"note": "короче"}, headers={"X-Panel": "1"})
    assert r.status_code == 202
    card = wait_revised(client, scene["scene_id"])
    assert card["review"]["revise_error"] == "Нет связи с DeepSeek 300 с"
    assert [l["text"] for l in card["script"]] == [l["text"] for l in scene["script"]]   # сценарий прежний


def test_videos_publish_texts_and_sequel(cfg):
    from pipeline.publish import save_publish
    client = panel_client(cfg)
    x = {"X-Panel": "1"}
    scene = make_scene(cfg, kind="imessage_drama", status="approved")
    sid = scene["scene_id"]
    manifest.record(cfg, scene, {"output": "output/videos/v.mp4", "duration_s": 55.0})
    clip = {"scene_id": str(uuid.uuid4()), "type": "video_clip", "created_at": "2026-09-15T10:00:00Z"}
    manifest.record(cfg, clip, {"output": "output/videos/c.mp4", "duration_s": 30.0})
    videos = {v["scene_id"]: v for v in client.get("/api/videos").json()}
    assert videos[sid]["writable"] and videos[sid]["publish"] is None and videos[sid]["part"] == 1
    assert not videos[clip["scene_id"]]["writable"]                               # у клипа сценария нет

    first = client.post(f"/api/videos/{sid}/publish", headers=x).json()
    again = client.post(f"/api/videos/{sid}/publish", headers=x).json()
    assert first["new"] and not again["new"] and first["job"] == again["job"]     # вторую такую же не ставим
    assert client.post(f"/api/videos/{clip['scene_id']}/publish", headers=x).status_code == 400
    assert client.post("/api/videos/publish-missing", headers=x).json()["queued"] == 0   # уже заказаны
    save_publish(cfg, sid, {"tiktok": "Он думал, это шутка #драма", "shorts_title": "Скрин #shorts",
                            "shorts_description": "Чем кончится?", "hashtags": ["#драма"]})
    assert client.get("/api/videos").json()[1]["publish"]["tiktok"] == "Он думал, это шутка #драма"

    r = client.post(f"/api/videos/{sid}/sequel", headers=x).json()
    queue = open_queue(cfg)
    job = queue.get(r["job"])
    queue.close()
    assert r["new"] and job.kind == "script_sequel" and job.payload["scene"].endswith(f"{sid}.json")
    summary = client.get("/api/summary").json()
    assert summary["videos"] == 2 and summary["telegram"] == {"configured": False, "connected": False,
                                                              "bot": None, "pair_link": None}


def test_approve_orders_publish_texts(cfg):
    scene = make_scene(cfg)
    queue = open_queue(cfg)
    approve(cfg, scene["scene_id"], queue)
    texts = queue.jobs("pending", kinds=("publish_meta",))
    queue.close()
    assert len(texts) == 1 and texts[0].payload["scene"].endswith(f"{scene['scene_id']}.json")


class TgResp:
    def __init__(self, data):
        self.data, self.status_code = data, 200

    def json(self):
        return self.data


class FakeTelegram:
    """Bot API без сети: updates — что «пришло» боту, calls — что бот отправил [(метод, параметры, файлы)]."""

    def __init__(self):
        self.calls, self.updates, self.message_id = [], [], 100

    def __call__(self, url, json=None, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, json if json is not None else data, files))
        if method == "getMe":
            result = {"username": "privatetiktokbot"}
        elif method == "getUpdates":
            result, self.updates = self.updates, []
        elif method in ("sendMessage", "sendVideo"):
            self.message_id += 1
            result = {"message_id": self.message_id}
        else:
            result = True
        return TgResp({"ok": True, "result": result})

    def sent(self, method):
        return [params for m, params, _ in self.calls if m == method]


def tg_message(update_id, text, chat=42, reply_to=None):
    msg = {"message_id": update_id, "chat": {"id": chat, "type": "private"}, "text": text}
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": msg}


def tg_button(update_id, data, chat=42, message_id=1):
    return {"update_id": update_id, "callback_query": {"id": f"q{update_id}", "from": {"id": chat}, "data": data,
                                                       "message": {"message_id": message_id, "chat": {"id": chat}}}}


def test_telegram_bot(cfg, monkeypatch):
    """Бот: подключение по коду из панели, сценарии с кнопками, правка ответом, ролик с текстами, чужие — мимо."""
    import json

    import requests

    import core.config
    from panel.telegram_bot import FarmBot
    from pipeline.publish import save_publish
    api = FakeTelegram()
    monkeypatch.setattr(requests, "post", api)
    core.config.ENV_FILE.write_text("TELEGRAM_BOT_TOKEN=<123456:TEST>\n", encoding="utf-8")   # в скобках — тоже
    old = make_scene(cfg, status="approved")
    manifest.record(cfg, old, {"output": "output/videos/old.mp4", "duration_s": 50.0})

    bot = FarmBot(cfg)
    bot.tick(poll_s=0)
    assert bot.info()["pair_link"] == f"https://t.me/privatetiktokbot?start={bot.pair_code}"
    api.updates = [tg_message(1, "/start 00000000", chat=666), tg_message(2, f"/start {bot.pair_code}")]
    bot.tick(poll_s=0)
    assert "TELEGRAM_CHAT_ID=42" in core.config.ENV_FILE.read_text(encoding="utf-8")
    assert [m["chat_id"] for m in api.sent("sendMessage")] == ["666", "42"]      # чужому — «подключается в панели»
    assert bot.info() == {"configured": True, "connected": True, "bot": "privatetiktokbot", "pair_link": None}

    scene = make_scene(cfg, kind="imessage_drama")
    sid = scene["scene_id"]
    bot.tick(poll_s=0)
    card = api.sent("sendMessage")[-1]
    assert "Лена:</b> Ты правда думала" in card["text"] and card["parse_mode"] == "HTML"
    assert [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row] == \
        [f"ap:{sid}", f"rj:{sid}", f"rw:{sid}"]
    bot.tick(poll_s=0)
    assert api.sent("sendMessage")[-1] is card                                    # второй раз не шлёт

    revised = []
    monkeypatch.setattr(panel.app, "revise_in_background", lambda cfg, sid, note: revised.append((sid, note)))
    api.updates = [tg_button(3, f"rw:{sid}")]
    bot.tick(poll_s=0)
    ask = api.message_id
    api.updates = [tg_message(4, "концовку жёстче", reply_to=ask), tg_message(5, "/status", chat=666)]
    bot.tick(poll_s=0)
    assert revised == [(sid, "концовку жёстче")] and load_scene(scene_path(cfg, sid))["meta"]["review"]["revising"]
    assert all(m["chat_id"] != "666" for m in api.sent("sendMessage")[2:])       # чужой /status — без ответа
    scene = load_scene(scene_path(cfg, sid))
    del scene["meta"]["review"]["revising"]
    save_scene(scene, scene_path(cfg, sid))

    api.updates = [tg_button(6, f"ap:{sid}", message_id=777)]
    bot.tick(poll_s=0)
    assert load_scene(scene_path(cfg, sid))["meta"]["review"]["status"] == "approved"
    assert api.sent("answerCallbackQuery")[-1]["text"].startswith("Одобрен")
    assert api.sent("editMessageReplyMarkup")[-1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "✅ Одобрен"

    videos = scene_path(cfg, sid).parents[1] / "videos"
    videos.mkdir(parents=True, exist_ok=True)
    (videos / "new.mp4").write_bytes(b"\x00" * 1000)
    manifest.record(cfg, load_scene(scene_path(cfg, sid)), {"output": "output/videos/new.mp4", "duration_s": 55.0})
    bot.tick(poll_s=0)
    assert api.sent("sendVideo") == []                                            # ждёт тексты для публикации
    save_publish(cfg, sid, {"tiktok": "Он думал, это шутка #драма", "shorts_title": "Скрин #shorts",
                            "shorts_description": "Чем кончится?", "hashtags": ["#драма"]})
    bot.tick(poll_s=0)
    method, params, files = [c for c in api.calls if c[0] == "sendVideo"][0]
    assert files["video"][0] == "new.mp4" and params["chat_id"] == "42" and "Скрин" in params["caption"]
    assert json.loads(params["reply_markup"])["inline_keyboard"][0][0] == {"text": "📺 Часть 2", "callback_data": f"sq:{sid}"}
    assert "<code>Он думал, это шутка #драма</code>" in api.sent("sendMessage")[-1]["text"]
    assert len(api.sent("sendVideo")) == 1                                        # старый ролик не прислан


def test_api_generate_loop(cfg):
    """Галочка «петля» у переписок: задачи сценариев с loop, у историй петли нет."""
    client = panel_client(cfg)
    x = {"X-Panel": "1"}
    r = client.post("/api/generate", json={"kind": "drama", "count": 2, "loop": True}, headers=x)
    queue = open_queue(cfg)
    assert [queue.get(i).payload for i in r.json()["jobs"]] == [{"topic": None, "loop": True}] * 2
    queue.close()
    assert client.post("/api/generate", json={"kind": "story", "loop": True}, headers=x).status_code == 400


def test_loop_card_and_video(cfg):
    client = panel_client(cfg)
    plain = make_scene(cfg, kind="imessage_drama", created="2026-09-15T09:00:00Z")
    scene = make_scene(cfg, kind="imessage_drama", created="2026-09-15T10:00:00Z")
    scene["meta"].update(loop=True, loop_idea="«О чём ты?» — и первое сообщение отвечает")
    save_scene(scene, scene_path(cfg, scene["scene_id"]))
    cards = client.get("/api/scenes?status=pending").json()
    assert [(c["scene_id"], c["loop"]) for c in cards] == [(scene["scene_id"], True), (plain["scene_id"], False)]
    assert cards[0]["loop_idea"].startswith("«О чём ты?»")

    manifest.record(cfg, scene, {"output": "output/videos/loop.mp4", "duration_s": 55.3})
    manifest.record(cfg, plain, {"output": "output/videos/plain.mp4", "duration_s": 57.0})
    videos = {v["scene_id"]: v for v in client.get("/api/videos").json()}
    assert videos[scene["scene_id"]]["loop"] is True and "loop" not in videos[plain["scene_id"]]


def test_watchdog_restarts_stopped_worker(cfg, monkeypatch):
    """Обработчик упал или ПК перезагрузился, а задачи остались — панель запускает его сама и пишет в Telegram."""
    import time
    started, sent = [], []
    monkeypatch.setattr(panel.app, "start_worker", lambda cfg: started.append(1) or True)
    monkeypatch.setattr(panel.app, "notify", lambda cfg, text: sent.append(text))
    assert panel.app.ensure_worker(cfg) == 0 and started == []                # очередь пуста — не нужен
    queue = open_queue(cfg)
    queue.enqueue("tts_synthesis", {})
    queue.enqueue("video_render", {})
    queue.close()
    monkeypatch.setattr(panel.app, "worker_running", lambda cfg: True)
    assert panel.app.ensure_worker(cfg) == 0 and started == []                # уже работает
    monkeypatch.setattr(panel.app, "worker_running", lambda cfg: False)
    with panel_client(cfg):                                # сторож стартует вместе с панелью
        for _ in range(100):
            if sent:
                break
            time.sleep(0.05)
    assert started == [1] and sent == ["⚠️ Обработчик очереди не работал — запущен снова. Задач в очереди: 2"]


def test_api_jobs_have_timestamps(cfg):
    client = panel_client(cfg)
    queue = open_queue(cfg)
    done_id, running_id, pending_id = (queue.enqueue("tts_synthesis", {}) for _ in range(3))
    queue.claim_next()
    queue.complete(done_id)
    queue.claim_next()
    queue.close()
    jobs = {j["id"]: j for j in client.get("/api/jobs").json()}
    assert all(jobs[done_id][k] for k in ("created_at", "started_at", "finished_at"))
    assert jobs[running_id]["started_at"] and not jobs[running_id]["finished_at"]
    assert not jobs[pending_id]["started_at"]


def test_api_password(cfg):
    """Вход по HTTP Basic оставлен для скриптов; людям — логин с паролем учётки."""
    cfg["panel"]["password"] = "секрет-123"
    client = TestClient(panel.app.create_app(cfg))       # без входа
    auth = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}  # noqa: E731
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers=auth("admin", "неверно")).status_code == 401
    assert client.get("/api/summary", headers=auth("admin", "секрет-123")).status_code == 200
    assert client.get("/").status_code == 200             # страница входа отдаётся без пароля


def test_login_required_and_session(cfg):
    """Без входа API закрыт, вход выдаёт куку сессии, выход её забирает."""
    client = TestClient(panel.app.create_app(cfg))
    x = {"X-Panel": "1"}
    assert client.get("/api/summary").status_code == 401
    assert client.post("/api/login", json={"login": "admin", "password": "не тот"}, headers=x).status_code == 401
    assert client.post("/api/login", json={"login": "admin", "password": "admin67"}, headers=x).status_code == 200
    me = client.get("/api/me").json()
    assert me["login"] == "admin" and me["role"] == "admin" and me["must_change_password"] is True
    assert client.get("/api/summary").status_code == 200
    assert client.post("/api/logout", headers=x).status_code == 200
    assert client.get("/api/summary").status_code == 401


def test_password_change_and_admin_only_users(cfg):
    admin = panel_client(cfg)
    x = {"X-Panel": "1"}
    assert admin.post("/api/me/password", json={"password": "коротк"}, headers=x).status_code == 200
    assert admin.post("/api/me/password", json={"password": "три"}, headers=x).status_code == 400   # короче 6
    # заводским паролем больше не войти, новым — да
    fresh = TestClient(panel.app.create_app(cfg))
    assert fresh.post("/api/login", json={"login": "admin", "password": "admin67"}, headers=x).status_code == 401
    assert fresh.post("/api/login", json={"login": "admin", "password": "коротк"}, headers=x).status_code == 200
    assert fresh.get("/api/me").json()["must_change_password"] is False

    assert len(admin.get("/api/users").json()) == 6                      # admin + user1..user5
    assert admin.post("/api/users", json={"login": "user1", "password": "другой"}, headers=x).status_code == 400
    assert admin.post("/api/users", json={"login": "vasya", "password": "парольвасi"}, headers=x).status_code == 200
    assert admin.delete("/api/users/admin", headers=x).status_code == 400  # себя удалить нельзя
    assert admin.delete("/api/users/vasya", headers=x).status_code == 200

    user = panel_client(cfg, "user1")                                    # обычный пользователь — не админ
    assert user.get("/api/users").status_code == 403
    assert user.post("/api/users", json={"login": "x", "password": "парольxx"}, headers=x).status_code == 403
    assert user.delete("/api/users/user2", headers=x).status_code == 403
    assert user.get("/api/summary").status_code == 200                   # но панелью пользуется как все


def test_media_only_from_videos_dir(cfg):
    client = panel_client(cfg)
    videos = work_dir(cfg, {"scene_id": "x"}).parents[1] / "videos"
    videos.mkdir(parents=True)
    (videos / "story_1.mp4").write_bytes(b"\x00" * 100)
    (videos.parent / "secret.mp4").write_bytes(b"nope")
    assert client.get("/media/videos/story_1.mp4").status_code == 200
    assert client.get("/media/videos/..%2Fsecret.mp4").status_code == 404
    assert client.get("/media/videos/notes.txt").status_code == 404
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/../app.py").status_code == 404


# --- разнообразие оформления и автоочистка -----------------------------------------------

def test_pick_style_deterministic_and_varied(dev_cfg):
    styles = [pick_style({"scene_id": str(uuid.UUID(int=i))}, dev_cfg) for i in range(40)]
    assert styles[0] == pick_style({"scene_id": str(uuid.UUID(int=0))}, dev_cfg)          # тот же scene_id — тот же вид
    assert len({s["font"] for s in styles}) >= 3 and len({s["highlight_color"] for s in styles}) >= 3
    fonts = {f["name"]: f["size"] for f in dev_cfg["subtitles"]["variety"]["fonts"]}
    assert all(fonts[s["font"]] == s["size"] for s in styles)

    saved = {"font": "Impact", "size": 108, "highlight_color": "#00E5FF"}
    assert pick_style({"scene_id": "x", "meta": {"style": saved}}, dev_cfg) == saved       # перерендер — сохранённый
    dev_cfg["subtitles"]["variety"]["enabled"] = False
    assert pick_style({"scene_id": "x"}, dev_cfg) == {}


def test_cleanup_after_render(cfg):
    scene = {"scene_id": str(uuid.uuid4())}
    folder = work_dir(cfg, scene)
    folder.mkdir(parents=True)
    for name in ("ui.mov", "voice.wav", "subs.ass"):
        (folder / name).write_bytes(b"\x00" * 1024)
    pipeline.jobs._cleanup(scene, cfg)
    assert sorted(p.name for p in folder.iterdir()) == ["subs.ass", "voice.wav"]           # озвучка остаётся
