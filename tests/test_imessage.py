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


def loop_scene():
    scene = voiced_scene()
    scene["meta"] = {"loop": True}
    return scene


def test_loop_timeline(dev_cfg):
    """Петля: без хвоста — пауза как между репликами, целое число кадров; «печатает…» перед первым
    сообщением следующего круга начинается в конце ролика."""
    tl = build_timeline(loop_scene(), dev_cfg)
    ic, fps = dev_cfg["imessage"], dev_cfg["video"]["fps"]
    frames = tl["total_ms"] * fps / 1000
    assert tl["loop"] is True and frames == pytest.approx(round(frames), abs=1e-3)
    assert tl["total_ms"] == pytest.approx(8500 + dev_cfg["tts"]["pause_between_lines_ms"], abs=1000 / fps)
    assert tl["wrap"]["side"] == "left"
    assert tl["wrap"]["typing_start_ms"] == pytest.approx(max(7000 + ic["min_gap_ms"], tl["total_ms"] - ic["typing_ms"]))
    assert "loop" not in build_timeline(voiced_scene(), dev_cfg) and "wrap" not in build_timeline(voiced_scene(), dev_cfg)


# Что видно на экране: пузыри (текст, место, хвостик), «печатает…» и «Прочитано»
_SCREEN = """t => {
  window.renderAt(t);
  const vp = document.getElementById("viewport").getBoundingClientRect();
  const rows = [...document.querySelectorAll(".row")].filter((r) => r.style.display !== "none").map((r) => {
    const b = r.firstChild.getBoundingClientRect();
    return { text: r.textContent, y: b.top - vp.top, x: b.left, last: r.classList.contains("last") };
  });
  const receipt = document.querySelector(".receipt");
  return { rows: rows.slice(-6), receipt: receipt.style.display === "none" ? null : receipt.textContent };
}"""


def test_loop_seam_is_invisible(dev_cfg):
    """Петля в браузере: последний кадр ролика — то же, что кадр перед первым (конец прошлого круга):
    те же пузыри на тех же местах и тот же «печатает…». После повтора переписка просто продолжается."""
    pytest.importorskip("playwright.sync_api")
    from playwright.sync_api import sync_playwright
    from imessage.recorder import WEB_DIR, _launch

    tl = build_timeline(loop_scene(), dev_cfg)
    ic, v = dev_cfg["imessage"], dev_cfg["video"]
    scale = v["width"] / ic["css_width"]
    frame = 1000 / v["fps"]
    with sync_playwright() as p:
        try:
            browser = _launch(p, ic["browser_channel"])
        except RuntimeError as e:
            pytest.skip(str(e))
        page = browser.new_page(viewport={"width": ic["css_width"],
                                          "height": round(dev_cfg["compositor"]["ui_height"] / scale)},
                                device_scale_factor=scale)
        page.goto((WEB_DIR / "index.html").as_uri())
        page.evaluate("data => window.init(data)", tl)
        before_first = page.evaluate(_SCREEN, -frame)
        last = page.evaluate(_SCREEN, tl["total_ms"] - frame)
        first = page.evaluate(_SCREEN, 0)
        browser.close()

    assert len(last["rows"]) == 6 and last["rows"][-1]["text"] == ""        # экран заполнен, собеседник печатает
    assert [(r["text"], r["last"]) for r in last["rows"]] == [(r["text"], r["last"]) for r in before_first["rows"]]
    for a, b in zip(last["rows"], before_first["rows"]):
        assert abs(a["y"] - b["y"]) <= 1 and a["x"] == b["x"]               # ±1 px — округление высоты списка
    assert last["receipt"] == before_first["receipt"]
    assert "сообщение 0" in first["rows"][-1]["text"]                       # и сразу — первое сообщение нового круга


def test_timeline_is_deterministic(dev_cfg):
    assert build_timeline(voiced_scene(), dev_cfg) == build_timeline(voiced_scene(), dev_cfg)


def test_timeline_requires_tts(dev_cfg):
    scene = voiced_scene()
    del scene["script"][0]["timestamp_ms"]
    with pytest.raises(ValueError, match="не озвучена"):
        build_timeline(scene, dev_cfg)
