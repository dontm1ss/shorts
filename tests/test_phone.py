"""Телефон: чистые функции без реального устройства — план прогрева, селекторы,
конфиг WireGuard, выбор устройства, путь к ролику."""
import pytest

from phone import PhoneError, accounts, device, proxy, selectors, warmup
from phone import post as phone_post


@pytest.fixture
def acc_cfg(dev_cfg, tmp_path):
    """Конфиг с файлом аккаунтов во временной папке — тесты не пишут в data/ репозитория."""
    dev_cfg["paths"]["phone_accounts"] = str(tmp_path / "phone-accounts.json")
    return dev_cfg


def test_phone_config_present(dev_cfg):
    phone = dev_cfg["phone"]
    assert phone["packages"]["tiktok"] == "com.zhiliaoapp.musically"
    assert phone["packages"]["youtube"] and phone["packages"]["instagram"]
    assert phone["remote_dir"].startswith("/sdcard/")
    for key in ("session_min_s", "session_max_s", "watch_min_s", "watch_max_s",
                "like_probability", "follow_probability"):
        assert key in phone["warmup"]


def test_warmup_plan_deterministic_and_in_bounds(dev_cfg):
    a = warmup.build_plan(dev_cfg, seed=42)
    b = warmup.build_plan(dev_cfg, seed=42)
    assert a == b and a
    w = dev_cfg["phone"]["warmup"]
    watches = [sec for action, sec in a if action == warmup.WATCH]
    assert all(w["watch_min_s"] <= sec <= w["watch_max_s"] for sec in watches)
    assert {action for action, _ in a} <= {warmup.WATCH, warmup.IDLE, warmup.SWIPE_UP,
                                           warmup.SWIPE_DOWN, warmup.LIKE, warmup.FOLLOW}


def test_warmup_plan_respects_like_probability_zero(dev_cfg):
    dev_cfg["phone"]["warmup"].update(like_probability=0.0, follow_probability=0.0)
    plan = warmup.build_plan(dev_cfg, seed=1)
    assert warmup.LIKE not in {action for action, _ in plan}
    assert warmup.FOLLOW not in {action for action, _ in plan}


def test_wireguard_conf_format():
    conf = proxy.wireguard_conf("iface-key", "srv-key", "1.2.3.4:51820", "10.8.0.2/32")
    assert conf.startswith("[Interface]\n")
    assert "PrivateKey = iface-key" in conf
    assert "PublicKey = srv-key" in conf
    assert "Endpoint = 1.2.3.4:51820" in conf
    assert "AllowedIPs = 0.0.0.0/0, ::/0" in conf


def test_selector_flows_cover_all_steps():
    recognized = {"text", "text_contains", "desc", "desc_contains", "resource_id", "xpath", "coord"}
    for platform in selectors.SELECTORS:
        steps = selectors.flow(platform)
        assert set(steps) == set(selectors.SELECTORS[platform]), platform
        for step in steps:
            candidates = selectors.SELECTORS[platform][step]
            assert candidates, f"{platform}.{step}: нет вариантов"
            for c in candidates:
                assert set(c) & recognized, f"{platform}.{step}: {c}"


def test_remote_path(dev_cfg):
    path = phone_post.remote_path(dev_cfg, __import__("pathlib").Path("clip_abc12345.mp4"))
    assert path == "/sdcard/Movies/ShortsFarm/clip_abc12345.mp4"


def test_find_video_unknown_scene(dev_cfg):
    with pytest.raises(PhoneError, match="Нет такого ролика"):
        phone_post.find_video(dev_cfg, "нет-такого")


def test_pick_serial_single_device(dev_cfg, monkeypatch):
    monkeypatch.setattr(device, "devices", lambda cfg: [
        {"serial": "RF8N", "state": "device", "model": "SM-A515F"}])
    assert device.pick_serial(dev_cfg) == "RF8N"
    assert device.pick_serial(dev_cfg, "forced") == "forced"   # явный серийник важнее


def test_pick_serial_requires_exactly_one(dev_cfg, monkeypatch):
    monkeypatch.setattr(device, "devices", lambda cfg: [])
    with pytest.raises(PhoneError, match="не подключён"):
        device.pick_serial(dev_cfg)
    monkeypatch.setattr(device, "devices", lambda cfg: [
        {"serial": "A", "state": "device"}, {"serial": "B", "state": "device"}])
    with pytest.raises(PhoneError, match="несколько"):
        device.pick_serial(dev_cfg)
    # конфигом phone.serial можно указать нужный
    dev_cfg["phone"]["serial"] = "B"
    assert device.pick_serial(dev_cfg) == "B"


def test_post_unknown_platform(dev_cfg):
    with pytest.raises(PhoneError, match="Неизвестная площадка"):
        phone_post.post(dev_cfg, "scene", "vkontakte")


def test_phone_jobs_registered():
    from pipeline.jobs import HANDLERS
    from pipeline.worker import PHONE_KINDS
    assert {"phone_post", "phone_warmup"} <= set(HANDLERS)
    assert {"phone_post", "phone_warmup"} <= set(PHONE_KINDS)   # телефон — своя линия


def test_warmup_selectors_present():
    for platform in warmup.PLATFORMS:
        table = selectors.WARMUP[platform]
        assert {"search_open", "search_field", "first_result"} <= set(table), platform
        if platform in ("youtube", "instagram"):
            assert table.get({"youtube": "shorts", "instagram": "reels"}[platform])


def test_accounts_defaults_and_warmup_transitions(acc_cfg):
    recs = accounts.all(acc_cfg)
    assert set(recs) == set(accounts.platforms(acc_cfg))
    assert all(r["status"] == "new" for r in recs.values())
    # одна короткая сессия: new → warming
    rec = accounts.record_warmup(acc_cfg, "tiktok", seconds=100, actions=5)
    assert rec["status"] == "warming" and rec["warmup_sessions"] == 1
    # набрали порог сессий — warming → warmed
    w = acc_cfg["phone"]["warmup"]
    for _ in range(int(w["sessions_to_warm"]) - 1):
        rec = accounts.record_warmup(acc_cfg, "tiktok", seconds=100)
    assert rec["status"] == "warmed" and rec["warmed_at"]
    # публикация переводит warmed → active
    rec = accounts.record_post(acc_cfg, "tiktok", "scene-1")
    assert rec["status"] == "active" and rec["posts"] == 1


def test_accounts_seconds_threshold_and_progress(acc_cfg):
    w = acc_cfg["phone"]["warmup"]
    accounts.record_warmup(acc_cfg, "youtube", seconds=int(w["seconds_to_warm"]))
    rec = accounts.get(acc_cfg, "youtube")
    assert rec["status"] == "warmed"
    assert accounts.progress(acc_cfg, rec)["percent"] == 100


def test_accounts_manual_status_kept(acc_cfg):
    accounts.set_status(acc_cfg, "instagram", "banned", notes="ушёл в бан")
    rec = accounts.get(acc_cfg, "instagram")
    assert rec["status"] == "banned" and rec["notes"] == "ушёл в бан"
    # прогрев не «оживляет» вручную выставленный статус
    accounts.record_warmup(acc_cfg, "instagram", seconds=5000)
    assert accounts.get(acc_cfg, "instagram")["status"] == "banned"
    with pytest.raises(PhoneError, match="статус"):
        accounts.set_status(acc_cfg, "instagram", "нет-такого")


@pytest.fixture
def panel_client(dev_cfg, tmp_path, monkeypatch):
    """Панель с временными путями и файлом аккаунтов — для проверки маршрутов телефона."""
    import panel.app as panel_app
    from fastapi.testclient import TestClient
    dev_cfg["paths"].update(
        output=str(tmp_path / "out"), manifest=str(tmp_path / "out" / "manifest.json"),
        users=str(tmp_path / "users.json"), panel_secret=str(tmp_path / "panel-secret"),
        social_links=str(tmp_path / "social-links.json"), uploads=str(tmp_path / "out" / "uploads.json"),
        phone_accounts=str(tmp_path / "phone-accounts.json"))
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["panel"]["password"] = None
    monkeypatch.setattr(panel_app, "start_worker", lambda cfg: False)
    app = TestClient(panel_app.create_app(dev_cfg))
    password = dev_cfg["panel"]["seed"]["admin"]["password"]
    assert app.post("/api/login", json={"login": "admin", "password": password},
                    headers={"X-Panel": "1"}).status_code == 200
    return app, dev_cfg


def test_panel_accounts_and_warmup(panel_client):
    from pipeline.worker import open_queue
    app, cfg = panel_client
    x = {"X-Panel": "1"}
    listed = app.get("/api/accounts").json()
    assert {a["platform"] for a in listed} == {"tiktok", "youtube", "instagram"}
    assert all(a["status"] == "new" and a["progress"]["percent"] == 0 for a in listed)
    first = app.post("/api/accounts/tiktok/warmup", headers=x).json()
    assert first["new"] is True
    assert app.post("/api/accounts/tiktok/warmup", headers=x).json()["new"] is False   # без дублей
    assert app.post("/api/accounts/вконтакте/warmup", headers=x).status_code == 404
    queue = open_queue(cfg)
    try:
        jobs = [j.payload for j in queue.jobs("pending", kinds=("phone_warmup",))]
    finally:
        queue.close()
    assert jobs == [{"platform": "tiktok"}]
    assert app.post("/api/accounts/tiktok/status", json={"status": "paused", "notes": "пауза"},
                    headers=x).json()["status"] == "paused"


def test_panel_phone_post_route(panel_client):
    from pipeline.manifest import _manifest_path, _save
    from pipeline.worker import open_queue
    app, cfg = panel_client
    x = {"X-Panel": "1"}
    # без ролика в манифесте — 404
    assert app.post("/api/videos/нет/phone-post", json={"platform": "tiktok"}, headers=x).status_code == 404
    _save(_manifest_path(cfg), [{"scene_id": "11111111-2222-3333-4444-555555555555", "type": "imessage_drama",
                                 "title": "Свадьба", "output": "output/videos/x.mp4"}])
    body = {"platform": "tiktok"}
    first = app.post("/api/videos/11111111-2222-3333-4444-555555555555/phone-post", json=body, headers=x).json()
    assert first["new"] is True
    again = app.post("/api/videos/11111111-2222-3333-4444-555555555555/phone-post", json=body, headers=x).json()
    assert again["new"] is False and again["job"] == first["job"]
    queue = open_queue(cfg)
    try:
        jobs = [j.payload for j in queue.jobs("pending", kinds=("phone_post",))]
    finally:
        queue.close()
    assert jobs == [{"scene_id": "11111111-2222-3333-4444-555555555555", "platform": "tiktok"}]
