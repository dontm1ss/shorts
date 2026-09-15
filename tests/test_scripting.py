"""DeepSeek-интеграция на подставном клиенте (без сети)."""
import pytest

from core.scene import scene_errors
from scripting import (DeepSeekClient, DeepSeekError, ScriptError, adapt_story, drama_scene,
                       estimate_duration_s, generate_drama, invent_story, rewrite_scene_length, story_scene,
                       word_budget, word_count)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, system, user, temperature=None):
        self.calls.append((system, user))
        return self.responses.pop(0)


STORY = {"title": "Табличка", "gender": "male",
         "parts": [{"emotion": "neutral", "text": "Три года сосед занимал моё место."},
                   {"emotion": "злость", "text": "Я повесил  табличку."},
                   {"emotion": "joy", "text": "Он сдался."}]}
# Старый формат ответа — только текст абзацами, без эмоций
OLD_STORY = {"title": "Табличка", "gender": "male",
             "text": "Три года сосед занимал моё место.\n\nЯ повесил табличку. Он сдался."}
DRAMA = {"title": "Скрин", "contacts": {"A": "Лена", "B": "Я"}, "genders": {"A": "Ж", "B": "female", "C": "male"},
         "messages": [{"speaker": "a", "emotion": "Angry", "text": "Ты правда думала, что я не узнаю?"},
                      {"speaker": "B", "text": "О чём ты?"}, {"speaker": "A", "text": "  "}]}


def test_word_count():
    assert word_count("Кто-то сказал: «три года» — и 2 раза ушёл.") == 8


def test_word_budget_accounts_for_pauses(dev_cfg):
    lo, target, hi = word_budget(dev_cfg)
    assert (lo, target, hi) == (169, 186, 203)                       # 3.4 слова/с × (50/55/60 с − 0.3 с)
    assert word_budget(dev_cfg, messages=20)[1] < target             # паузы между сообщениями съедают время


def test_adapt_story_prompt_and_result(dev_cfg):
    client = FakeClient(STORY)
    story = adapt_story({"title": "Neighbor", "text": "He parked in my spot."}, client, dev_cfg)
    system, user = client.calls[0]
    assert "154–188 слова" in system and "словами" in system             # бюджет с паузами между фрагментами
    assert '"gender"' in system                                          # пол рассказчика — для голоса
    assert '"parts"' in system and "- anger — злость" in system          # интонация фрагментов для озвучки
    assert "He parked in my spot." in user
    assert [p["emotion"] for p in story["parts"]] == ["neutral", "anger", "joy"]   # «злость» → anger
    assert story["text"] == "Три года сосед занимал моё место.\n\nЯ повесил табличку.\n\nОн сдался."


def test_story_old_format_is_neutral(dev_cfg):
    story = adapt_story({"title": "", "text": "x"}, FakeClient(OLD_STORY), dev_cfg)
    assert [p["emotion"] for p in story["parts"]] == ["neutral", "neutral"]     # абзацы без эмоций
    assert story["text"] == OLD_STORY["text"]


@pytest.mark.parametrize("raw, emotion", [("anger", "anger"), ("ANGER", "anger"), ("злость", "anger"),
                                          ("surprised", "shock"), ("восторг", "joy"), ("???", "neutral"),
                                          (None, "neutral")])
def test_normalize_emotion(raw, emotion):
    from core.emotions import normalize_emotion
    assert normalize_emotion(raw) == emotion


def test_emotions_match_schema_and_config(dev_cfg):
    """Список эмоций один: core.emotions = enum схемы = имена записей = tts.emotions."""
    import json
    from core.emotions import EMOTIONS
    from core.scene import SCHEMA_PATH
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["properties"]["script"]["items"]["properties"]["emotion"]["enum"] == list(EMOTIONS)
    assert list(dev_cfg["tts"]["emotions"]) == list(EMOTIONS)
    assert all(set(v) <= {"exaggeration", "cfg_weight", "temperature"} for v in dev_cfg["tts"]["emotions"].values())


@pytest.mark.parametrize("raw, gender", [("male", "male"), ("Женский", "female"), ("ж", "female"),
                                         ("unknown", None), (None, None)])
def test_story_gender_normalized(dev_cfg, raw, gender):
    story = adapt_story({"title": "", "text": "x"}, FakeClient({**STORY, "gender": raw}), dev_cfg)
    assert story["gender"] == gender
    scene = story_scene(story, dev_cfg)
    assert scene_errors(scene) == []
    assert scene["meta"]["genders"] == ({"narrator": gender} if gender else {})


def test_adapt_story_rejects_empty(dev_cfg):
    for empty in ({"title": "t", "text": " "}, {"title": "t", "parts": [{"emotion": "anger", "text": " "}]}):
        with pytest.raises(ScriptError):
            adapt_story({"title": "", "text": "x"}, FakeClient(empty), dev_cfg)


def test_story_scene(dev_cfg):
    scene = story_scene(STORY, dev_cfg, {"id": "reddit:abc"})
    assert scene_errors(scene) == []
    assert [l["speaker"] for l in scene["script"]] == ["narrator"] * 3           # фрагмент — реплика
    assert [l["emotion"] for l in scene["script"]] == ["neutral", "anger", "joy"]
    assert scene["script"][1]["text"] == "Я повесил табличку."
    assert scene["meta"]["source"]["id"] == "reddit:abc"
    assert scene["output"]["path"].startswith("output/videos/reddit_story_")
    old = story_scene(OLD_STORY, dev_cfg)                                          # абзац — реплика
    assert [(l["emotion"], l["text"]) for l in old["script"]] == [
        ("neutral", "Три года сосед занимал моё место."), ("neutral", "Я повесил табличку. Он сдался.")]


def test_drama_normalized(dev_cfg):
    client = FakeClient(DRAMA)
    drama = generate_drama(client, dev_cfg, topic="измена")
    assert "Тема истории: измена" in client.calls[0][1]
    assert '"emotion"' in client.calls[0][0] and "- sarcasm — сарказм" in client.calls[0][0]
    assert [m["speaker"] for m in drama["messages"]] == ["A", "B"]      # регистр исправлен, пустое убрано
    assert [m["emotion"] for m in drama["messages"]] == ["anger", "neutral"]   # «Angry» → anger, нет — neutral
    scene = drama_scene(drama, dev_cfg)
    assert scene_errors(scene) == [] and scene["meta"]["contacts"]["A"] == "Лена"
    assert [l["emotion"] for l in scene["script"]] == ["anger", "neutral"]
    assert scene["meta"]["genders"] == {"A": "female", "B": "female"}     # «Ж» распознан, чужая роль C убрана


def test_drama_rejects_unknown_speaker(dev_cfg):
    bad = {"messages": [{"speaker": "C", "text": "кто я"}]}
    with pytest.raises(ScriptError, match="отправитель"):
        generate_drama(FakeClient(bad), dev_cfg)


def test_estimate_duration(dev_cfg):
    script = [{"text": "слово " * 24}, {"text": "слово " * 24}]
    assert estimate_duration_s(script, dev_cfg) == pytest.approx(48 / 3.4 + 0.35 + 2 * 0.3)


def test_rewrite_keeps_identity(dev_cfg):
    scene = story_scene(STORY, dev_cfg)
    longer = {"title": "Табличка", "parts": STORY["parts"] + [{"emotion": "shock", "text": "И ещё абзац."}]}
    client = FakeClient(longer)
    new = rewrite_scene_length(scene, current_s=30.0, client=client, cfg=dev_cfg)
    assert new["scene_id"] == scene["scene_id"] and new["output"] == scene["output"]
    assert [l["emotion"] for l in new["script"]] == ["neutral", "anger", "joy", "shock"]
    system, user = client.calls[0]
    assert "расширь" in system and '"emotion"' in system
    assert '"emotion": "anger"' in user                        # редактор видит интонации фрагментов
    assert new["meta"]["genders"] == {"narrator": "male"}      # в ответе пола нет — остаётся прежний


def test_rewrite_drama_direction(dev_cfg):
    scene = drama_scene(generate_drama(FakeClient(DRAMA), dev_cfg), dev_cfg)
    client = FakeClient(DRAMA)
    new = rewrite_scene_length(scene, current_s=75.0, client=client, cfg=dev_cfg)
    assert "сократи" in client.calls[0][0]
    assert '"emotion": "anger"' in client.calls[0][1]
    assert [l["emotion"] for l in new["script"]] == ["anger", "neutral"]


def test_invent_story_without_post(dev_cfg):
    """Основной путь: историю DeepSeek придумывает сам — без поста-оригинала."""
    client = FakeClient(STORY)
    story = invent_story(client, dev_cfg)
    system, user = client.calls[0]
    assert "превращать исходный материал" in system and "НЕПРЕДСКАЗУЕМОСТЬ" in system
    assert "Тема конфликта" in system and "Второй главный персонаж" in system
    assert user.startswith("Исходного материала нет")
    assert "ИНТОНАЦИЯ ОЗВУЧКИ" in system
    assert len(story["parts"]) == 3 and story["text"].startswith("Три года")

    client = FakeClient(STORY)
    invent_story(client, dev_cfg, topic="сосед сверху")
    system, user = client.calls[0]
    assert "сосед сверху" in user and "Тема конфликта" not in system   # не спорит с темой


def test_invented_story_scene_keeps_topic(dev_cfg):
    scene = story_scene(STORY, dev_cfg, topic="сосед сверху")
    assert scene["meta"]["topic"] == "сосед сверху" and scene["meta"]["source"] == {"source": "deepseek"}
    longer = {"title": "Т", "gender": "male", "text": OLD_STORY["text"] + "\n\nЕщё абзац."}   # ответ без parts
    new = rewrite_scene_length(scene, current_s=30.0, client=FakeClient(longer), cfg=dev_cfg)
    assert new["meta"]["topic"] == "сосед сверху"


def test_script_story_job_invents(dev_cfg, tmp_path, monkeypatch):
    import scripting
    from pipeline import jobs
    from core.scene import load_scene
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    monkeypatch.setattr(scripting, "DeepSeekClient", lambda cfg: FakeClient(STORY, STORY, STORY))
    monkeypatch.setattr(jobs, "_prefit", lambda scene, cfg, client: scene)
    result = jobs.script_story({"topic": "  соседи  "}, dev_cfg)
    scene = load_scene(tmp_path / "out" / "scenes" / (result["scene"].rsplit("/", 1)[-1]))
    assert result["review"] == "pending" and scene["meta"]["topic"] == "соседи"


def test_client_requires_key(dev_cfg):
    dev_cfg["deepseek"]["api_key"] = None
    with pytest.raises(DeepSeekError, match=".env"):
        DeepSeekClient(dev_cfg)


class FakeStream:
    """Ответ DeepSeek потоком (SSE): строки data: {...}, пустые строки и «: keep-alive»."""

    def __init__(self, lines, status=200):
        self.lines, self.status_code, self.text = lines, status, ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        yield from self.lines


def sse(*texts):
    import json
    chunks = [json.dumps({"choices": [{"delta": {"content": t}}]}, ensure_ascii=False).encode() for t in texts]
    return [b": keep-alive", b""] + [b"data: " + c for c in chunks] + [b"data: [DONE]"]


def test_client_restarts_hung_request(dev_cfg, monkeypatch):
    """DeepSeek при перегрузке держит соединение пустыми строками вместо ответа — перезапуск."""
    import requests
    import scripting.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 10))                        # каждая строка — +10 с
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: next(clock))
    responses = [FakeStream([b""] * 20),                          # 200 с одних пустых строк
                 FakeStream([], status=503),
                 FakeStream(sse('{"title": ', '"Скрин"}'))]
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(kw) or responses.pop(0))
    dev_cfg["deepseek"]["api_key"] = "test"
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"title": "Скрин"}
    assert len(calls) == 3 and calls[0]["json"]["stream"] is True


def test_client_keeps_long_answer_and_gives_up(dev_cfg, monkeypatch):
    import requests
    import scripting.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 20))                        # текст идёт медленно: 20 с на кусок
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeStream(sse('{"a"', ": ", "1", "}")))
    dev_cfg["deepseek"]["api_key"] = "test"
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"a": 1}     # 80 с, но ответ шёл — не оборван

    import json
    thinking = [b"data: " + json.dumps({"choices": [{"delta": {"reasoning_content": "хм"}}]}).encode()] * 6
    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeStream(thinking + sse('{"b": 2}')))
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"b": 2}     # v4-pro 2 минуты думала — не оборвана

    monkeypatch.setattr(requests, "post", lambda *a, **kw: FakeStream([b""] * 20))
    with pytest.raises(DeepSeekError, match="нет ответа за 60 с"):
        DeepSeekClient(dev_cfg).chat_json("s", "u")


@pytest.fixture
def fake_api(dev_cfg, monkeypatch):
    """Подставной API: ответы по очереди, запросы — в calls (модель — calls[i]["json"]["model"])."""
    import requests
    import scripting.client as client_mod
    monkeypatch.setattr(client_mod.time, "sleep", lambda s: None)
    clock = iter(range(0, 1_000_000, 10))                      # каждая строка ответа — +10 с
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: next(clock))
    dev_cfg["deepseek"]["api_key"] = "test"
    api = type("Api", (), {"responses": [], "calls": []})()
    monkeypatch.setattr(requests, "post", lambda *a, **kw: api.calls.append(kw["json"]["model"]) or api.responses.pop(0))
    return api


def test_flash_first_pro_only_when_flash_fails(dev_cfg, fake_api):
    """Основная — flash; v4-pro — только если flash не ответил, и дальше, пока идёт пауза."""
    from pathlib import Path
    fake_api.responses += [FakeStream(sse('{"ok": 1}'))]
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 1}
    assert fake_api.calls == ["deepseek-flash"]                            # flash работает — только он

    fake_api.calls.clear()
    fake_api.responses += [FakeStream([b""] * 20), FakeStream(sse('{"ok": 2}'))]   # flash висит
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 2}
    assert fake_api.calls == ["deepseek-flash", "deepseek-v4-pro"]

    fake_api.calls.clear()
    fake_api.responses += [FakeStream(sse('{"ok": 3}'))]
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 3}          # следующая задача — другой процесс
    assert fake_api.calls == ["deepseek-v4-pro"]                           # пауза: минуту на flash не ждём

    Path(dev_cfg["paths"]["deepseek_state"]).write_text('{"primary_down_until": 0}', encoding="utf-8")
    fake_api.calls.clear()
    fake_api.responses += [FakeStream(sse('{"ok": 4}'))]
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 4}
    assert fake_api.calls == ["deepseek-flash"]                            # пауза прошла — снова flash


def test_network_outage_waited_out_without_fallback(dev_cfg, monkeypatch):
    """Обрыв VPN: ProxyError не тратит попытки и не переключает на v4-pro — ждём связь."""
    from pathlib import Path

    import requests
    import scripting.client as client_mod
    sleeps = []
    monkeypatch.setattr(client_mod.time, "sleep", sleeps.append)
    clock = iter(range(0, 1_000_000, 10))
    monkeypatch.setattr(client_mod.time, "monotonic", lambda: next(clock))
    dev_cfg["deepseek"]["api_key"] = "test"
    responses, models = [], []

    def post(*a, **kw):
        models.append(kw["json"]["model"])
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(requests, "post", post)
    responses += [requests.exceptions.ProxyError("VPN")] * 5 + [FakeStream(sse('{"ok": 1}'))]
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 1}
    assert models == ["deepseek-flash"] * 6                                # 5 обрывов > max_retries = 3
    assert sleeps == [5, 10, 20, 30, 30]
    assert not Path(dev_cfg["paths"]["deepseek_state"]).exists()          # flash не помечен сломанным

    responses[:] = [requests.exceptions.ConnectionError("нет сети")] * 100
    with pytest.raises(DeepSeekError, match="Нет связи с DeepSeek 300 с"):
        DeepSeekClient(dev_cfg).chat_json("s", "u")                       # связь так и не вернулась


def test_fallback_on_flash_error_not_on_bad_key(dev_cfg, fake_api):
    from pathlib import Path
    state = Path(dev_cfg["paths"]["deepseek_state"])
    fake_api.responses += [FakeStream([], status=400), FakeStream(sse('{"ok": 1}'))]   # «Model Not Exist»
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 1}
    assert fake_api.calls == ["deepseek-flash", "deepseek-v4-pro"] and state.exists()

    state.unlink()
    fake_api.calls.clear()
    fake_api.responses += [FakeStream(sse("не json")), FakeStream(sse('{"ok": 2}'))]
    assert DeepSeekClient(dev_cfg).chat_json("s", "u") == {"ok": 2}
    assert fake_api.calls == ["deepseek-flash", "deepseek-v4-pro"]
    assert not state.exists()                                  # один битый ответ flash не отключает

    fake_api.calls.clear()
    fake_api.responses += [FakeStream([], status=401)]
    with pytest.raises(DeepSeekError, match="401"):
        DeepSeekClient(dev_cfg).chat_json("s", "u")            # неверный ключ — запасная не поможет
    assert fake_api.calls == ["deepseek-flash"]
