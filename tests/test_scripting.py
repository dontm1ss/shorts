"""DeepSeek-интеграция на подставном клиенте (без сети)."""
import pytest

from core.scene import scene_errors
from scripting import (DeepSeekClient, DeepSeekError, ScriptError, adapt_story, drama_scene,
                       estimate_duration_s, generate_drama, rewrite_scene_length, story_scene,
                       word_budget, word_count)


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def chat_json(self, system, user, temperature=None):
        self.calls.append((system, user))
        return self.responses.pop(0)


STORY = {"title": "Табличка", "text": "Три года сосед занимал моё место.\n\nЯ повесил табличку. Он сдался."}
DRAMA = {"title": "Скрин", "contacts": {"A": "Лена", "B": "Я"},
         "messages": [{"speaker": "a", "text": "Ты правда думала, что я не узнаю?"},
                      {"speaker": "B", "text": "О чём ты?"}, {"speaker": "A", "text": "  "}]}


def test_word_count():
    assert word_count("Кто-то сказал: «три года» — и 2 раза ушёл.") == 8


def test_word_budget_accounts_for_pauses(dev_cfg):
    lo, target, hi = word_budget(dev_cfg)
    assert (lo, target, hi) == (179, 197, 215)                       # 3.6 слова/с × (50/55/60 с − 0.3 с)
    assert word_budget(dev_cfg, messages=20)[1] < target             # паузы между сообщениями съедают время


def test_adapt_story_prompt_and_result(dev_cfg):
    client = FakeClient(STORY)
    story = adapt_story({"title": "Neighbor", "text": "He parked in my spot."}, client, dev_cfg)
    system, user = client.calls[0]
    assert "179–215 слов" in system and "50–60 секунд" in system and "словами" in system
    assert "He parked in my spot." in user
    assert story == STORY


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


def test_drama_rejects_unknown_speaker(dev_cfg):
    bad = {"messages": [{"speaker": "C", "text": "кто я"}]}
    with pytest.raises(ScriptError, match="отправитель"):
        generate_drama(FakeClient(bad), dev_cfg)


def test_estimate_duration(dev_cfg):
    script = [{"text": "слово " * 24}, {"text": "слово " * 24}]
    assert estimate_duration_s(script, dev_cfg) == pytest.approx(48 / 3.6 + 0.35 + 2 * 0.3)


def test_rewrite_keeps_identity(dev_cfg):
    scene = story_scene(STORY, dev_cfg)
    longer = {"title": "Табличка", "text": STORY["text"] + "\n\nИ ещё абзац."}
    client = FakeClient(longer)
    new = rewrite_scene_length(scene, current_s=30.0, client=client, cfg=dev_cfg)
    assert new["scene_id"] == scene["scene_id"] and new["output"] == scene["output"]
    assert len(new["script"]) == 3
    assert "расширь" in client.calls[0][0]


def test_rewrite_drama_direction(dev_cfg):
    scene = drama_scene(generate_drama(FakeClient(DRAMA), dev_cfg), dev_cfg)
    client = FakeClient(DRAMA)
    rewrite_scene_length(scene, current_s=75.0, client=client, cfg=dev_cfg)
    assert "сократи" in client.calls[0][0]


def test_client_requires_key(dev_cfg):
    dev_cfg["deepseek"]["api_key"] = None
    with pytest.raises(DeepSeekError, match=".env"):
        DeepSeekClient(dev_cfg)
