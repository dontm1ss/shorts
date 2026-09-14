"""Отбор сценариев, веб-панель, разнообразие оформления субтитров, автоочистка."""
import base64
import uuid

import pytest
from fastapi.testclient import TestClient

import panel.app
import pipeline.jobs
from core.scene import load_scene, save_scene, scene_path, work_dir
from pipeline.review import (ReviewError, approve, list_scenes, mark_pending, processing_chain, reject, restore,
                             set_texts)
from pipeline.worker import open_queue
from subtitles.scene import pick_style


@pytest.fixture
def cfg(dev_cfg, tmp_path, monkeypatch):
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    dev_cfg["paths"]["manifest"] = str(tmp_path / "out" / "manifest.json")
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["panel"]["password"] = None
    monkeypatch.setattr(panel.app, "start_worker", lambda cfg: False)   # без реального фонового процесса
    return dev_cfg


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


# --- API панели ---------------------------------------------------------------------

def test_api_review_flow(cfg):
    client = TestClient(panel.app.create_app(cfg))
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
    client = TestClient(panel.app.create_app(cfg))
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


def test_api_jobs_have_timestamps(cfg):
    client = TestClient(panel.app.create_app(cfg))
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
    cfg["panel"]["password"] = "секрет-123"
    client = TestClient(panel.app.create_app(cfg))
    auth = lambda u, p: {"Authorization": "Basic " + base64.b64encode(f"{u}:{p}".encode()).decode()}  # noqa: E731
    assert client.get("/api/summary").status_code == 401
    assert client.get("/api/summary", headers=auth("admin", "неверно")).status_code == 401
    assert client.get("/api/summary", headers=auth("admin", "секрет-123")).status_code == 200
    assert client.get("/", headers=auth("admin", "секрет-123")).status_code == 200


def test_media_only_from_videos_dir(cfg):
    client = TestClient(panel.app.create_app(cfg))
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
