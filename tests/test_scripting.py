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
         "text": "Три года сосед занимал моё место.\n\nЯ повесил табличку. Он сдался."}
DRAMA = {"title": "Скрин", "contacts": {"A": "Лена", "B": "Я"}, "genders": {"A": "Ж", "B": "female", "C": "male"},
         "messages": [{"speaker": "a", "text": "Ты правда думала, что я не узнаю?"},
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
    assert "169–203 слова" in system and "словами" in system
    assert '"gender"' in system                                          # пол рассказчика — для голоса
    assert "He parked in my spot." in user
    assert story == STORY


@pytest.mark.parametrize("raw, gender", [("male", "male"), ("Женский", "female"), ("ж", "female"),
                                         ("unknown", None), (None, None)])
def test_story_gender_normalized(dev_cfg, raw, gender):
    story = adapt_story({"title": "", "text": "x"}, FakeClient({**STORY, "gender": raw}), dev_cfg)
    assert story["gender"] == gender
    scene = story_scene(story, dev_cfg)
    assert scene_errors(scene) == []
    assert scene["meta"]["genders"] == ({"narrator": gender} if gender else {})


def test_adapt_story_rejects_empty(dev_cfg):
    with pytest.raises(ScriptError):
        adapt_story({"title": "", "text": "x"}, FakeClient({"title": "t", "text": " "}), dev_cfg)


def test_story_scene(dev_cfg):
    scene = story_scene(STORY, dev_cfg, {"id": "reddit:abc"})
    assert scene_errors(scene) == []
    assert [l["speaker"] for l in scene["script"]] == ["narrator", "narrator"]   # абзац — реплика
    assert scene["meta"]["source"]["id"] == "reddit:abc"
    assert scene["output"]["path"].startswith("output/videos/reddit_story_")


def test_drama_normalized(dev_cfg):
    client = FakeClient(DRAMA)
    drama = generate_drama(client, dev_cfg, topic="измена")
    assert "Тема истории: измена" in client.calls[0][1]
    assert [m["speaker"] for m in drama["messages"]] == ["A", "B"]      # регистр исправлен, пустое убрано
    scene = drama_scene(drama, dev_cfg)
    assert scene_errors(scene) == [] and scene["meta"]["contacts"]["A"] == "Лена"
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
    longer = {"title": "Табличка", "text": STORY["text"] + "\n\nИ ещё абзац."}
    client = FakeClient(longer)
    new = rewrite_scene_length(scene, current_s=30.0, client=client, cfg=dev_cfg)
    assert new["scene_id"] == scene["scene_id"] and new["output"] == scene["output"]
    assert len(new["script"]) == 3
    assert "расширь" in client.calls[0][0]
    assert new["meta"]["genders"] == {"narrator": "male"}      # в ответе пола нет — остаётся прежний


def test_rewrite_drama_direction(dev_cfg):
    scene = drama_scene(generate_drama(FakeClient(DRAMA), dev_cfg), dev_cfg)
    client = FakeClient(DRAMA)
    rewrite_scene_length(scene, current_s=75.0, client=client, cfg=dev_cfg)
    assert "сократи" in client.calls[0][0]


def test_invent_story_without_post(dev_cfg):
    """Основной путь: историю DeepSeek придумывает сам — без поста-оригинала."""
    client = FakeClient(STORY)
    story = invent_story(client, dev_cfg)
    system, user = client.calls[0]
    assert "превращать исходный материал" in system and "НЕПРЕДСКАЗУЕМОСТЬ" in system
    assert "Тема конфликта" in system and "Второй главный персонаж" in system
    assert user.startswith("Исходного материала нет")
    assert story["text"] == STORY["text"]

    client = FakeClient(STORY)
    invent_story(client, dev_cfg, topic="сосед сверху")
    system, user = client.calls[0]
    assert "сосед сверху" in user and "Тема конфликта" not in system   # не спорит с темой


def test_invented_story_scene_keeps_topic(dev_cfg):
    scene = story_scene(STORY, dev_cfg, topic="сосед сверху")
    assert scene["meta"]["topic"] == "сосед сверху" and scene["meta"]["source"] == {"source": "deepseek"}
    longer = {"title": "Т", "gender": "male", "text": STORY["text"] + "\n\nЕщё абзац."}
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
