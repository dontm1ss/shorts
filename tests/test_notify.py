"""Уведомления в Telegram (без сети: подставной requests.post), сводки обработчика, настройка чата."""
import json
import logging

import pytest
import requests

import core.config
import notify.digest
import notify.telegram
import pipeline.worker
from notify import Digest, notify as send_notify
from pipeline.queue import Job

TOKEN = "123456:TEST-token"


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class Calls(list):
    """Запросы к Bot API [(url, json)]; results — ответы по методу (getMe, getUpdates…)."""
    results: dict


@pytest.fixture
def posts(monkeypatch):
    calls = Calls()
    calls.results = {}

    def post(url, json=None, timeout=None):
        calls.append((url, json))
        return Resp({"ok": True, "result": calls.results.get(url.rsplit("/", 1)[-1], {"message_id": 1})})

    monkeypatch.setattr(requests, "post", post)
    return calls


def env(**values):
    core.config.ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


def job(kind, scene=None, job_id=7):
    return Job(id=job_id, kind=kind, payload={"scene": str(scene)} if scene else {}, status="done", depends_on=None,
               attempts=1, max_attempts=2, result=None, error=None, created_at="", started_at=None, finished_at=None)


def test_not_configured_sends_nothing(dev_cfg, posts):
    assert send_notify(dev_cfg, "привет") is False and posts == []                  # нет токена и чата
    env(TELEGRAM_BOT_TOKEN=TOKEN)
    assert send_notify(dev_cfg, "привет") is False and posts == []                  # нет чата


def test_sends_to_owner_chat(dev_cfg, posts):
    env(DEEPSEEK_API_KEY="sk-x", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")   # .env читается при отправке
    assert send_notify(dev_cfg, "Готово") is True
    url, body = posts[0]
    assert url == f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    assert body == {"chat_id": "42", "text": "Готово", "disable_web_page_preview": True}
    dev_cfg["notify"]["enabled"] = False
    assert send_notify(dev_cfg, "Готово") is False and len(posts) == 1


def test_failure_logged_without_token(dev_cfg, monkeypatch, caplog):
    env(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="42")

    def down(url, **kw):
        raise requests.exceptions.ConnectionError(f"Max retries exceeded with url: {url}")

    monkeypatch.setattr(requests, "post", down)
    with caplog.at_level(logging.WARNING):
        assert send_notify(dev_cfg, "x") is False                                   # конвейер не падает
    assert "не отправлено" in caplog.text and TOKEN not in caplog.text and "***" in caplog.text
    monkeypatch.setattr(requests, "post", lambda url, **kw: Resp({"ok": False, "description": "chat not found"}, 400))
    assert send_notify(dev_cfg, "x") is False and "chat not found" in caplog.text


def test_digest_batches_failures(dev_cfg, tmp_path):
    scene = tmp_path / "s.json"
    scene.write_text(json.dumps({"type": "imessage_drama", "meta": {"title": "Скрин"}}), encoding="utf-8")
    sent, now = [], [0.0]
    digest = Digest(dev_cfg, send=lambda cfg, text: sent.append(text), clock=lambda: now[0])
    digest.job_failed(job("tts_synthesis", scene, 5), "Traceback …\nRuntimeError: CUDA out of memory\n")
    digest.job_failed(job("script_drama", job_id=6), "DeepSeekError: Нет связи с DeepSeek 300 с")
    digest.tick(idle=True)
    assert sent == []                                                  # ждём, не упадёт ли что-то ещё
    now[0] = dev_cfg["notify"]["failures_batch_s"]
    digest.tick(idle=False)
    assert len(sent) == 1 and sent[0].startswith("❌ Не удалось задач: 2")
    assert "#5 озвучка «Скрин»: RuntimeError: CUDA out of memory" in sent[0]
    assert "#6 сценарий переписки: DeepSeekError: Нет связи с DeepSeek 300 с" in sent[0]
    assert sent[0].endswith("Панель: https://v3242400.hosted-by-vdsina.ru")


def test_digest_ready_when_queue_empties(dev_cfg, tmp_path):
    dev_cfg["notify"]["bot"] = False                                   # без бота панели — сводка от обработчика
    scene = tmp_path / "s.json"
    scene.write_text(json.dumps({"type": "imessage_drama", "meta": {"title": "Четверг, баня", "loop": True}}),
                     encoding="utf-8")
    sent, now = [], [0.0]
    digest = Digest(dev_cfg, send=lambda cfg, text: sent.append(text), clock=lambda: now[0])
    digest.job_done(job("video_render", scene), {"duration_s": 57.8})
    digest.job_done(job("script_story"), {"scene": "x", "review": "pending"})
    digest.job_done(job("script_story"), {"scene": "y", "next": [[]]})                # автоодобрен — не на проверке
    digest.job_done(job("tts_synthesis", scene), {})
    digest.tick(idle=False)
    assert sent == []                                                  # работа идёт — сводка позже
    digest.tick(idle=True)
    assert sent == ["✅ Готово роликов: 1\n• «Четверг, баня» — переписка ↻, 58 с\n\n"
                    "📝 Новых сценариев на проверке: 1\n\nПанель: https://v3242400.hosted-by-vdsina.ru"]
    digest.job_done(job("video_render", scene), {"duration_s": 50})
    now[0] = dev_cfg["notify"]["ready_digest_min"] * 60
    digest.tick(idle=False)                                            # очередь не пустеет — раз в час
    assert len(sent) == 2
    dev_cfg["notify"]["bot"] = True                                    # бот панели шлёт ролики сам, по одному
    digest.job_done(job("video_render", scene), {"duration_s": 50})
    digest.job_done(job("script_story"), {"review": "pending"})
    digest.flush()
    assert len(sent) == 2


def test_digest_never_breaks_worker(dev_cfg, caplog):
    def broken(cfg, text):
        raise RuntimeError("сбой отправки")

    digest = Digest(dev_cfg, send=broken, clock=lambda: 10 ** 6)
    digest.job_failed(job("video_render"), "boom")
    with caplog.at_level(logging.ERROR):
        digest.tick(idle=True)
        digest.flush()
    assert "Сводка для Telegram" in caplog.text


def test_worker_reports_final_failures_only(dev_cfg, tmp_path, monkeypatch):
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    q = pipeline.worker.open_queue(dev_cfg)
    q.enqueue("video_render", {})
    q.close()
    sent = []
    monkeypatch.setattr(notify.digest, "notify", lambda cfg, text: sent.append(text))
    monkeypatch.setattr(pipeline.worker, "_run_subprocess",
                        lambda jobs, cfg: {j.id: (False, {}, "RuntimeError: nvenc") for j in jobs})
    assert pipeline.worker.run_worker(dev_cfg, poll_s=0.01) == {"failed": 1}
    assert len(sent) == 1 and "#1 рендер: RuntimeError: nvenc" in sent[0]   # две попытки — одно сообщение


def test_setup_finds_chat_and_saves_it(dev_cfg, posts, capsys):
    from notify.__main__ import main, private_chats
    updates = [{"message": {"chat": {"id": 1, "type": "private", "first_name": "Андрей"}}},
               {"message": {"chat": {"id": -5, "type": "group"}}},
               {"message": {"chat": {"id": 2, "type": "private", "first_name": "Чужой", "username": "x"}}},
               {"message": {"chat": {"id": 1, "type": "private", "first_name": "Андрей"}}}]
    assert [c["id"] for c in private_chats(updates)] == [1, 2]                     # последний написавший — первым

    env(DEEPSEEK_API_KEY="sk-x", TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID="old")
    posts.results = {"getMe": {"username": "privatetiktokbot"}, "getUpdates": updates[:1]}
    assert main(["setup"]) == 0
    text = core.config.ENV_FILE.read_text(encoding="utf-8")
    assert text == f"DEEPSEEK_API_KEY=sk-x\nTELEGRAM_BOT_TOKEN={TOKEN}\nTELEGRAM_CHAT_ID=1\n"   # старый чат заменён
    assert posts[-1][1]["chat_id"] == "1" and "подключены" in posts[-1][1]["text"]

    posts.results["getUpdates"] = updates                                          # писали двое — выбрать самому
    assert main(["setup"]) == 1 and "--chat ID" in capsys.readouterr().out
    assert main(["setup", "--chat", "2"]) == 0 and "TELEGRAM_CHAT_ID=2" in core.config.ENV_FILE.read_text()
