import pytest

from imessage import build_timeline


def voiced_scene():
    starts = [0, 2400, 4200, 7000]
    script = [{"speaker": s, "text": f"сообщение {i}", "timestamp_ms": t, "duration_ms": 1500}
              for i, (s, t) in enumerate(zip("ABAA", starts))]
    return {"scene_id": "8a2e6d40-1c3b-4f5a-b7e9-6d0c2f4a9b33", "type": "imessage_drama",
            "script": script, "audio": {"path": "x.wav", "duration_ms": 8500}}


def test_timeline(dev_cfg):
    tl = build_timeline(voiced_scene(), dev_cfg)
    msgs = tl["messages"]
    assert [m["side"] for m in msgs] == ["left", "right", "left", "left"]    # B — владелец, справа
    assert [m["appear_ms"] for m in msgs] == [0, 2400, 4200, 7000]           # синхронно с озвучкой
    for prev, m in zip(msgs, msgs[1:]):
        assert prev["appear_ms"] + dev_cfg["imessage"]["min_gap_ms"] <= m["typing_start_ms"] <= m["appear_ms"]
    assert msgs[3]["typing_start_ms"] == 7000 - dev_cfg["imessage"]["typing_ms"]
    assert tl["total_ms"] == 8500 + dev_cfg["compositor"]["tail_ms"]
    assert tl["stamp_day"] == "Сегодня" and len(tl["stamp_time"]) == 5


def test_timeline_is_deterministic(dev_cfg):
    assert build_timeline(voiced_scene(), dev_cfg) == build_timeline(voiced_scene(), dev_cfg)


def test_timeline_requires_tts(dev_cfg):
    scene = voiced_scene()
    del scene["script"][0]["timestamp_ms"]
    with pytest.raises(ValueError, match="не озвучена"):
        build_timeline(scene, dev_cfg)
