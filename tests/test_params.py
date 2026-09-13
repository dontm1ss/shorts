"""Генератор параметров разнообразия: выбор без недавних повторов, память, формат промпта."""
from scripting import params


def test_pick_avoids_recent_when_possible():
    pool = ["a", "b", "c"]
    assert params._pick(pool, ["a", "b"])[0] == "c"


def test_pick_falls_back_to_full_pool_if_all_recent():
    pool = ["a", "b"]
    assert params._pick(pool, ["a", "b"])[0] in pool


def test_story_params_from_declared_pools(dev_cfg):
    p = params.story_params(dev_cfg)
    assert p["opening"] in params.OPENINGS_STORY
    assert p["tone"] in params.TONES_STORY
    assert p["ending"] in params.ENDINGS_STORY
    assert p["recent_hooks"] == []                     # история пуста


def test_drama_params_two_distinct_archetypes_and_twists(dev_cfg):
    p = params.drama_params(dev_cfg)
    assert p["archetype_a"] != p["archetype_b"]
    assert p["speech_a"] != p["speech_b"]
    assert p["twist_1"] != p["twist_2"]
    assert {p["archetype_a"], p["archetype_b"]} <= set(params.ARCHETYPES)


def test_record_and_recall_story(dev_cfg):
    chosen = params.story_params(dev_cfg)
    params.record_story(dev_cfg, "Первая фраза истории. Дальше текст.", chosen)
    again = params.story_params(dev_cfg)
    assert again["recent_hooks"] == ["Первая фраза истории."]


def test_record_trims_to_history_len(dev_cfg):
    for i in range(params.HISTORY_LEN + 3):
        params.record_story(dev_cfg, f"Фраза номер {i}.", params.story_params(dev_cfg))
    hist = params._load_history(dev_cfg)["reddit_story"]
    assert len(hist) == params.HISTORY_LEN
    assert hist[-1]["hook"] == f"Фраза номер {params.HISTORY_LEN + 2}."


def test_avoid_last_reduces_immediate_repeats(dev_cfg):
    seen = []
    for _ in range(20):
        p = params.story_params(dev_cfg)
        params.record_story(dev_cfg, "x.", p)
        seen.append(p["opening"])
    # AVOID_LAST < len(pool) — соседние генерации не должны совпадать по завязке
    assert all(seen[i] != seen[i + 1] for i in range(len(seen) - 1))


def test_format_story_variety_includes_rules_and_hooks(dev_cfg):
    p = params.story_params(dev_cfg)
    p["recent_hooks"] = ["Старая завязка."]
    text = params.format_story_variety(p)
    assert "Не копируй структуру предыдущих историй" in text
    assert p["opening"] in text and p["tone"] in text and p["ending"] in text
    assert "Старая завязка." in text


def test_format_drama_variety_includes_both_twists(dev_cfg):
    p = params.drama_params(dev_cfg)
    text = params.format_drama_variety(p)
    assert p["twist_1"] in text and p["twist_2"] in text
    assert p["archetype_a"] in text and p["speech_b"] in text


def test_hook_takes_first_sentence_only():
    assert params._hook("Первое предложение. Второе предложение.") == "Первое предложение."
