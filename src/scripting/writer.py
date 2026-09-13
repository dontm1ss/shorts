"""Сценарии через DeepSeek: адаптация форумной истории, iMessage-переписка, подгонка длины.

Длина задаётся в словах через темп озвучки (story.words_per_second); точный хронометраж
50–60 с проверяется после TTS, и при промахе текст переписывается (pipeline.stages).
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

from core.scene import rel_path, validate_scene, video_path
from scripting import params, prompts
from scripting.client import DeepSeekClient

_WORD = re.compile(r"[^\W\d_]+(?:-[^\W\d_]+)*|\d+")
DRAMA_MESSAGES = (14, 26)
_EST_DRAMA_MESSAGES = 20
_GENDER_ALIASES = {"female": "female", "f": "female", "woman": "female", "женский": "female", "ж": "female",
                   "male": "male", "m": "male", "man": "male", "мужской": "male", "м": "male"}


def normalize_gender(value) -> str | None:
    """Пол героя из ответа DeepSeek → female | male; непонятное — None (голос по умолчанию)."""
    return _GENDER_ALIASES.get(str(value or "").strip().lower())


def _genders(raw: dict) -> dict:
    """{роль: пол} только с распознанным полом."""
    return {role: g for role, value in (raw or {}).items() if (g := normalize_gender(value))}


class ScriptError(ValueError):
    pass


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


def _overhead_s(cfg: dict, lines: int) -> float:
    """Время сверх самих слов: паузы между репликами + хвосты каждой реплики."""
    return (lines - 1) * cfg["tts"]["pause_between_lines_ms"] / 1000 + lines * cfg["story"]["line_overhead_s"]


def word_budget(cfg: dict, messages: int = 1) -> tuple[int, int, int]:
    """(min, target, max) слов под 50–60 с с учётом пауз между репликами."""
    s, wps = cfg["story"], cfg["story"]["words_per_second"]
    overhead = _overhead_s(cfg, messages)
    return tuple(max(1, round((sec - overhead) * wps))
                 for sec in (s["min_duration_s"], s["target_duration_s"], s["max_duration_s"]))


def estimate_duration_s(script: list[dict], cfg: dict) -> float:
    words = sum(word_count(line["text"]) for line in script)
    return words / cfg["story"]["words_per_second"] + _overhead_s(cfg, len(script))


def _limits(cfg: dict, messages: int = 1) -> dict:
    lo, _, hi = word_budget(cfg, messages)
    s = cfg["story"]
    return {"min_words": lo, "max_words": hi, "min_s": s["min_duration_s"], "max_s": s["max_duration_s"],
            "format_rules": prompts.FORMAT_RULES}


# --- проверка ответов -----------------------------------------------------------

def _check_story(data: dict) -> dict:
    if not isinstance(data.get("text"), str) or not data["text"].strip():
        raise ScriptError(f"DeepSeek вернул историю без текста: {str(data)[:200]}")
    return {"title": str(data.get("title", "")).strip()[:80], "gender": normalize_gender(data.get("gender")),
            "text": data["text"].strip()}


def _check_drama(data: dict) -> dict:
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ScriptError(f"DeepSeek вернул переписку без сообщений: {str(data)[:200]}")
    clean = []
    for m in messages:
        speaker = str(m.get("speaker", "")).strip().upper()
        text = str(m.get("text", "")).strip()
        if speaker not in ("A", "B"):
            raise ScriptError(f"Неизвестный отправитель {speaker!r} в сообщении {m}")
        if text:
            clean.append({"speaker": speaker, "text": text})
    contacts = data.get("contacts") or {}
    return {"title": str(data.get("title", "")).strip()[:80],
            "contacts": {"A": str(contacts.get("A") or "Контакт"), "B": str(contacts.get("B") or "Я")},
            "genders": _genders({k: v for k, v in (data.get("genders") or {}).items() if k in ("A", "B")}),
            "messages": clean}


# --- генерация ------------------------------------------------------------------

def adapt_story(post: dict, client: DeepSeekClient, cfg: dict) -> dict:
    """Пост форума {title, text} → {title, text} на русском под 50–60 с.

    Завязка/тон/развязка каждый раз задаются случайными параметрами (scripting.params),
    чтобы истории не выходили однотипными — DeepSeek просто пересказывает суть поста,
    а не изобретает структуру сам."""
    story_params = params.story_params(cfg)
    system = prompts.STORY_SYSTEM.format(**_limits(cfg), variety=params.format_story_variety(story_params))
    user = prompts.STORY_USER.format(title=post.get("title", ""), text=post["text"][:12000])
    story = _check_story(client.chat_json(system, user))
    params.record_story(cfg, story["text"], story_params)
    return story


def generate_drama(client: DeepSeekClient, cfg: dict, topic: str | None = None) -> dict:
    drama_params = params.drama_params(cfg)
    limits = _limits(cfg, _EST_DRAMA_MESSAGES)
    system = prompts.DRAMA_SYSTEM.format(**limits, min_messages=DRAMA_MESSAGES[0], max_messages=DRAMA_MESSAGES[1],
                                         variety=params.format_drama_variety(drama_params))
    user = prompts.DRAMA_USER_TOPIC.format(topic=topic) if topic else prompts.DRAMA_USER_FREE
    drama = _check_drama(client.chat_json(system, user))
    if drama["messages"]:
        params.record_drama(cfg, drama["messages"][0]["text"], drama_params)
    return drama


def _direction(current_s: float, target_s: float) -> str:
    return "сократи" if current_s > target_s else "расширь, добавив деталей и эмоций,"


def _ask_words(current_words: int, current_s: float, target_s: float) -> int:
    """Сколько слов просить. LLM стабильно недотягивает при расширении и перебирает при
    сокращении — просим с запасом."""
    bias = 1.1 if current_s < target_s else 0.95
    return round(current_words * target_s / current_s * bias)


def adjust_story(story: dict, current_s: float, client: DeepSeekClient, cfg: dict) -> dict:
    target_s = cfg["story"]["target_duration_s"]
    current_words = word_count(story["text"])
    target_words = _ask_words(current_words, current_s, target_s)
    system = prompts.ADJUST_STORY_SYSTEM.format(direction=_direction(current_s, target_s),
                                                target_words=target_words, current_words=current_words,
                                                format_rules=prompts.FORMAT_RULES)
    return _check_story(client.chat_json(system, f"Заголовок: {story['title']}\n\n{story['text']}", 0.7))


def adjust_drama(drama: dict, current_s: float, client: DeepSeekClient, cfg: dict) -> dict:
    target_s = cfg["story"]["target_duration_s"]
    n = len(drama["messages"])
    speech_now = max(1.0, current_s - _overhead_s(cfg, n))
    speech_target = target_s - _overhead_s(cfg, n)
    current_words = sum(word_count(m["text"]) for m in drama["messages"])
    target_words = _ask_words(current_words, speech_now, speech_target)
    system = prompts.ADJUST_DRAMA_SYSTEM.format(direction=_direction(current_s, target_s),
                                                target_words=target_words, current_words=current_words,
                                                format_rules=prompts.FORMAT_RULES)
    return _check_drama(client.chat_json(system, json.dumps(drama, ensure_ascii=False), 0.7))


# --- сцены ----------------------------------------------------------------------

def _new_scene(scene_type: str, cfg: dict) -> dict:
    scene_id = str(uuid.uuid4())
    v = cfg["video"]
    return {"scene_id": scene_id, "type": scene_type,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "output": {"resolution": f"{v['width']}x{v['height']}",
                       "path": rel_path(video_path(cfg, scene_type, scene_id))}}


def story_scene(story: dict, cfg: dict, source: dict | None = None) -> dict:
    scene = _new_scene("reddit_story", cfg)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", story["text"]) if p.strip()]
    scene["script"] = [{"speaker": "narrator", "text": " ".join(p.split())} for p in paragraphs]
    scene["meta"] = {"title": story["title"], "source": source or {},
                     "genders": _genders({"narrator": story.get("gender")})}
    return validate_scene(scene)


def drama_scene(drama: dict, cfg: dict, topic: str | None = None) -> dict:
    scene = _new_scene("imessage_drama", cfg)
    scene["script"] = [{"speaker": m["speaker"], "text": m["text"]} for m in drama["messages"]]
    scene["meta"] = {"title": drama["title"], "contacts": drama["contacts"], "topic": topic or "",
                     "genders": _genders(drama.get("genders"))}
    return validate_scene(scene)


def scene_story(scene: dict) -> dict:
    """Обратно из сцены — для переписывания под хронометраж."""
    meta = scene.get("meta", {})
    return {"title": meta.get("title", ""), "gender": meta.get("genders", {}).get("narrator"),
            "text": "\n\n".join(line["text"] for line in scene["script"])}


def scene_drama(scene: dict) -> dict:
    meta = scene.get("meta", {})
    return {"title": meta.get("title", ""), "contacts": meta.get("contacts", {"A": "Контакт", "B": "Я"}),
            "genders": meta.get("genders", {}),
            "messages": [{"speaker": l["speaker"], "text": l["text"]} for l in scene["script"]]}


def rewrite_scene_length(scene: dict, current_s: float, client: DeepSeekClient, cfg: dict) -> dict:
    """Переписывает сценарий сцены под target_duration_s. Возвращает новую (неозвученную) сцену
    с тем же scene_id."""
    if scene["type"] == "imessage_drama":
        new = drama_scene(adjust_drama(scene_drama(scene), current_s, client, cfg), cfg,
                          scene.get("meta", {}).get("topic"))
    else:
        new = story_scene(adjust_story(scene_story(scene), current_s, client, cfg), cfg,
                          scene.get("meta", {}).get("source"))
    new.update(scene_id=scene["scene_id"], created_at=scene["created_at"], output=scene["output"])
    # пол героев не меняется при переписывании — голос озвучки остаётся тем же
    new["meta"]["genders"] = {**new["meta"].get("genders", {}), **scene.get("meta", {}).get("genders", {})}
    return validate_scene(new)
