"""Манифест готовых роликов (output/manifest.json): запись и разметка по формату сцены."""
import json

from pipeline.manifest import record


def _cfg(dev_cfg, tmp_path):
    dev_cfg["paths"]["manifest"] = str(tmp_path / "manifest.json")
    return dev_cfg


def test_record_reddit_story(dev_cfg, tmp_path):
    cfg = _cfg(dev_cfg, tmp_path)
    scene = {"scene_id": "s1", "type": "reddit_story", "created_at": "2026-09-13T12:00:00Z",
             "meta": {"title": "Табличка", "source": {"source": "pikabu", "url": "https://pikabu.ru/x"}}}
    record(cfg, scene, {"duration_s": 54.321, "output": "output/videos/reddit_story_s1.mp4"})
    entries = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert entries == [{
        "scene_id": "s1", "type": "reddit_story", "title": "Табличка", "source": "pikabu",
        "url": "https://pikabu.ru/x", "duration_s": 54.3, "output": "output/videos/reddit_story_s1.mp4",
        "created_at": "2026-09-13T12:00:00Z", "rendered_at": entries[0]["rendered_at"],
    }]


def test_record_imessage_drama_uses_topic_as_url(dev_cfg, tmp_path):
    cfg = _cfg(dev_cfg, tmp_path)
    scene = {"scene_id": "s2", "type": "imessage_drama", "created_at": "",
             "meta": {"title": "Скрин", "topic": "измена"}}
    record(cfg, scene, {"duration_s": 59.0, "output": "output/videos/imessage_drama_s2.mp4"})
    entry = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))[0]
    assert entry["source"] == "deepseek" and entry["url"] == "измена" and entry["title"] == "Скрин"


def test_record_video_clip_uses_source_path(dev_cfg, tmp_path):
    cfg = _cfg(dev_cfg, tmp_path)
    scene = {"scene_id": "s3", "type": "video_clip", "created_at": "",
             "source_video": {"path": "data/source-videos/film.mp4", "clip_start_ms": 0, "clip_end_ms": 30000}}
    record(cfg, scene, {"duration_s": 30.0, "output": "output/videos/video_clip_s3.mp4"})
    entry = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))[0]
    assert entry["title"] == "" and entry["source"] == "data/source-videos/film.mp4"


def test_record_appends_without_overwriting(dev_cfg, tmp_path):
    cfg = _cfg(dev_cfg, tmp_path)
    for i in range(3):
        record(cfg, {"scene_id": f"s{i}", "type": "video_clip", "created_at": ""},
               {"duration_s": 1.0, "output": f"o{i}.mp4"})
    entries = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [e["scene_id"] for e in entries] == ["s0", "s1", "s2"]


def test_rerender_replaces_entry(dev_cfg, tmp_path):
    """Перерендер той же сцены — тот же файл ролика: в «Готовых» он не должен задваиваться."""
    cfg = _cfg(dev_cfg, tmp_path)
    for scene_id, duration in (("s0", 1.0), ("s1", 1.0), ("s0", 2.0)):
        record(cfg, {"scene_id": scene_id, "type": "video_clip", "created_at": ""},
               {"duration_s": duration, "output": f"{scene_id}.mp4"})
    entries = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert [(e["scene_id"], e["duration_s"]) for e in entries] == [("s1", 1.0), ("s0", 2.0)]
