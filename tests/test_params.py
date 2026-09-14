"""Генератор параметров разнообразия: выбор без недавних повторов, память, формат промпта."""
from scripting import params, prompts


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
    assert p["rhythm"] in params.RHYTHMS_STORY
    assert p["reveal"] in params.REVEALS
    assert p["finale"] in params.FINALES
    assert p["recent_hooks"] == []                     # история пуста


def test_modes_never_contradict(dev_cfg):
    """Два режима «иногда» — всегда из разных групп: не «оба долбоёбы» + «один очевидно неправ»."""
    group_of = {m: i for i, group in enumerate(params.MODE_GROUPS) for m in group}
    for _ in range(200):
        modes = params.story_params(dev_cfg)["modes"]
        assert len(modes) == 2 and group_of[modes[0]] != group_of[modes[1]]


def test_drama_params_distinct_and_variable_twists(dev_cfg):
    counts = set()
    for _ in range(60):
        p = params.drama_params(dev_cfg)
        assert p["archetype_a"] != p["archetype_b"] and p["speech_a"] != p["speech_b"]
        assert {p["archetype_a"], p["archetype_b"]} <= set(params.ARCHETYPES)
        assert len(set(p["twists"])) == len(p["twists"]) and set(p["twists"]) <= set(params.TWISTS)
        assert p["rhythm"] in params.RHYTHMS_DRAMA
        counts.add(len(p["twists"]))
    assert counts == {1, 2, 3}                          # «меняй количество поворотов»


def test_record_and_recall_story(dev_cfg):
    chosen = params.story_params(dev_cfg)
    params.record_story(dev_cfg, "Первая фраза истории. Дальше текст.", chosen)
    again = params.story_params(dev_cfg)
    assert again["recent_hooks"] == ["Первая фраза истории."]


def test_recent_modes_avoided(dev_cfg):
    first = params.story_params(dev_cfg)
    params.record_story(dev_cfg, "x.", first)
    assert set(params._recent(dev_cfg, "reddit_story", "modes")) == set(first["modes"])


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


def test_format_story_variety(dev_cfg):
    p = params.story_params(dev_cfg)
    p["recent_hooks"] = ["Старая завязка."]
    text = params.format_story_variety(p)
    assert p["opening"] in text and p["tone"] in text and p["finale"] in text
    assert all(m in text for m in p["modes"]) and p["rhythm"] in text and p["reveal"] in text
    assert "Старая завязка." in text


def test_format_drama_variety(dev_cfg):
    p = params.drama_params(dev_cfg)
    text = params.format_drama_variety(p)
    assert all(t in text for t in p["twists"])
    assert p["archetype_a"] in text and p["speech_b"] in text and p["conflict"] in text
    # тема от пользователя — случайная «тема конфликта» не подставляется, чтобы не спорить с ней
    assert "Тема конфликта" not in params.format_drama_variety(p, topic="подруга слила переписку")


def test_owner_prompts_with_config_numbers():
    """Промпты владельца: числа длины — подстановки из конфига, в конце — параметры генерации."""
    for template in (prompts.STORY_SYSTEM, prompts.DRAMA_SYSTEM):
        assert "{variety}" in template and "{min_words}–{max_words}" in template
        assert "НЕПРЕДСКАЗУЕМОСТЬ" in template and "ТЕХНИЧЕСКИЕ ОГРАНИЧЕНИЯ" in template
    assert "{min_messages}–{max_messages}" in prompts.DRAMA_SYSTEM
    assert "«Блять, я вообще не туда смотрел»" in prompts.STORY_SYSTEM
    assert "«Блять, так вот что это всё значило»" in prompts.DRAMA_SYSTEM
    assert "169" not in prompts.STORY_SYSTEM and "127" not in prompts.DRAMA_SYSTEM   # не зашиты


def test_hook_takes_first_sentence_only():
    assert params._hook("Первое предложение. Второе предложение.") == "Первое предложение."
