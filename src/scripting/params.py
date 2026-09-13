"""Генератор параметров для разнообразия историй и переписок.

Проблема: даже с temperature=1.0 DeepSeek тяготеет к одинаковым завязкам, архетипам
персонажей и способам развязки. Решение — не просить «пиши разнообразно» в общих
словах, а на каждую генерацию:
  1) случайно выбрать конкретные параметры (как начать, чей архетип, чья манера речи,
     какие повороты, как закончить) из пулов ниже, стараясь не повторять то, что
     выбиралось в последних generation'ах того же формата (data/scripting-history.json);
  2) явно перечислить в промпте, чем не должен быть похож новый текст на несколько
     последних (реальные первые фразы прошлых историй/переписок).

История хранится в JSON-файле — воркер каждую задачу запускает отдельным процессом
(CLAUDE.md), в памяти между генерациями ничего не переживает.
"""
from __future__ import annotations

import json
import os
import random
import re
from pathlib import Path

from core.config import ROOT

HISTORY_LEN = 8     # сколько последних генераций каждого формата помнить (для подсказки «не так, как здесь»)
AVOID_LAST = 4      # на столько последних генераций не повторять один и тот же вариант параметра

VARIETY_RULES = """\
1. Не копируй структуру предыдущих историй.
2. Не повторяй одинаковую завязку.
3. Не используй одинаковый порядок развития событий.
4. Не начинай постоянно с конфликта.
5. Не используй одинаковые фразы.
6. Не используй одинаковые типы персонажей.
7. Не заканчивай истории одинаковым способом.
8. Добавь минимум 2 неожиданных изменения ситуации.
9. Диалог должен выглядеть естественно.
10. Каждый персонаж должен иметь собственную манеру общения."""

# --- пулы вариантов (истории от первого лица) ------------------------------------

OPENINGS_STORY = [
    "с обычного, ничем не примечательного момента, который потом всё меняет",
    "с прямой речи одного из героев — сразу цитата, без предисловий",
    "с итога или результата, а дальше — как до этого дошло",
    "с внутренней мысли рассказчика, а не с события",
    "с описания места и настроения, само событие — через пару фраз",
    "с неожиданного вопроса, который задаёт кто-то из героев",
    "с воспоминания из прошлого, которое возвращается к текущим событиям",
    "с бытовой детали, которая позже окажется важной",
]

TONES_STORY = [
    "сухой сарказм", "самоирония", "тревожно и сбивчиво, будто выговаривается",
    "спокойно и отстранённо, будто пересказывает чужую историю", "на грани срыва, очень эмоционально",
    "чёрный юмор", "устало и разочарованно", "тепло, с ноткой ностальгии",
]

ENDINGS_STORY = [
    "на итоге без морали — пусть слушатель сам решает, кто прав",
    "ироничной деталью", "коротким выводом рассказчика", "открытым вопросом к себе",
    "неожиданной фразой другого персонажа", "тем, что изменилось после",
]

# --- пулы вариантов (переписка iMessage) ------------------------------------------

OPENINGS_DRAMA = [
    "с обычного бытового сообщения — конфликт проявляется не сразу",
    "с шутки или флирта, которые потом оборачиваются иначе",
    "с прямого обвинения без объяснений — детали всплывают дальше",
    "с просьбы о помощи",
    "с пересланного сообщения или скриншота, о котором один из героев не знал",
    "с вопроса ни о чём — разговор постепенно уходит в сторону",
    "с извинения, причина которого не сразу понятна",
    "с новости, которая меняет планы героев",
]

CONFLICT_DOMAINS = [
    "измена", "наследство", "долг и деньги", "переезд и жильё", "тайна из прошлого",
    "ревность к друзьям", "работа и карьера", "воспитание детей", "подготовка к свадьбе",
    "предательство доверия", "конфликт поколений", "личные границы", "зависимость",
    "ложь во благо", "чужие ожидания", "развод",
]

ARCHETYPES = [
    "наивный романтик", "циничный карьерист", "заботливый, но властный контролёр",
    "холодный перфекционист", "добродушный разгильдяй", "манипулятор-нарцисс",
    "тревожный отличник", "прямолинейный правдоруб", "пассивно-агрессивный молчун",
    "харизматичный лжец", "гиперответственный старший", "вечная жертва обстоятельств",
    "саркастичный наблюдатель", "импульсивный максималист", "холодный расчётливый прагматик",
    "ранимый идеалист",
]

SPEECH_MANNERS = [
    "короткие рубленые фразы, почти без знаков препинания",
    "длинные оправдывающиеся сообщения с кучей деталей",
    "сарказм и подколки почти в каждой реплике",
    "постоянные вопросы, как будто допрашивает",
    "сухо и по делу, без эмоций",
    "рваные эмоциональные фразы с восклицаниями",
    "мягкая манера, уменьшительно-ласкательные слова",
    "грубовато и прямо, без сглаживания",
    "любит многоточия и недосказанность",
    "пишет уверенно и слегка свысока",
]

TWISTS = [
    "выясняется, что виноват на самом деле третий персонаж",
    "всплывает информация из прошлого, которая меняет смысл событий",
    "герой, который казался правым, сам оказывается неправ",
    "вмешивается кто-то посторонний и переворачивает разговор",
    "случайное совпадение объясняет то, что казалось предательством",
    "то, что выглядело обманом, оказывается попыткой защитить",
    "герой узнаёт правду не от собеседника, а случайно",
    "прошлое решение одного из героев неожиданно аукается именно сейчас",
]

ENDINGS_DRAMA = [
    "жёсткий разрыв без примирения",
    "неожиданное примирение вопреки всему",
    "открытый финал — неясно, чем всё закончится",
    "ирония судьбы: герой получает то, чего добивался, но не рад",
    "герой уходит из разговора, оставив только короткое сообщение",
    "чёрный юмор в последней реплике",
    "герой блокирует собеседника",
    "признание, которое всё меняет, но уже поздно",
]


def _history_file(cfg: dict) -> Path:
    return ROOT / cfg["paths"]["scripting_history"]


def _load_history(cfg: dict) -> dict[str, list[dict]]:
    path = _history_file(cfg)
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_history(cfg: dict, history: dict) -> None:
    path = _history_file(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)   # атомарно, как data/processed-forum-ids.json


def _pick(pool: list[str], recent: list[str], n: int = 1) -> list[str]:
    """n различных элементов из pool, по возможности не встречавшихся в recent."""
    candidates = [x for x in pool if x not in recent] or list(pool)
    if len(candidates) < n:
        candidates = list(pool)
    return random.sample(candidates, n)


def _recent(cfg: dict, kind: str, key: str, limit: int = AVOID_LAST) -> list[str]:
    hist = _load_history(cfg).get(kind, [])[-limit:]
    return [h["params"][key] for h in hist if key in h.get("params", {})]


def _recent_hooks(cfg: dict, kind: str) -> list[str]:
    return [h["hook"] for h in _load_history(cfg).get(kind, [])[-HISTORY_LEN:] if h.get("hook")]


def story_params(cfg: dict) -> dict:
    """Параметры для новой истории от первого лица + крючки последних историй."""
    return {
        "opening": _pick(OPENINGS_STORY, _recent(cfg, "reddit_story", "opening"))[0],
        "tone": _pick(TONES_STORY, _recent(cfg, "reddit_story", "tone"))[0],
        "ending": _pick(ENDINGS_STORY, _recent(cfg, "reddit_story", "ending"))[0],
        "recent_hooks": _recent_hooks(cfg, "reddit_story"),
    }


def drama_params(cfg: dict) -> dict:
    """Параметры для новой переписки + крючки последних переписок."""
    recent_archetypes = _recent(cfg, "imessage_drama", "archetype_a") + _recent(cfg, "imessage_drama", "archetype_b")
    recent_speech = _recent(cfg, "imessage_drama", "speech_a") + _recent(cfg, "imessage_drama", "speech_b")
    archetype_a, archetype_b = _pick(ARCHETYPES, recent_archetypes, n=2)
    speech_a, speech_b = _pick(SPEECH_MANNERS, recent_speech, n=2)
    twist_1, twist_2 = _pick(TWISTS, [], n=2)
    return {
        "opening": _pick(OPENINGS_DRAMA, _recent(cfg, "imessage_drama", "opening"))[0],
        "conflict": _pick(CONFLICT_DOMAINS, _recent(cfg, "imessage_drama", "conflict"))[0],
        "archetype_a": archetype_a, "archetype_b": archetype_b,
        "speech_a": speech_a, "speech_b": speech_b,
        "twist_1": twist_1, "twist_2": twist_2,
        "ending": _pick(ENDINGS_DRAMA, _recent(cfg, "imessage_drama", "ending"))[0],
        "recent_hooks": _recent_hooks(cfg, "imessage_drama"),
    }


def _format_variety_block(params: dict, extra_lines: list[str]) -> str:
    lines = "\n".join(f"- {line}" for line in extra_lines)
    block = f"Разнообразие — обязательно, не как в предыдущих генерациях:\n{VARIETY_RULES}\n\nПараметры именно этого текста:\n{lines}"
    if params["recent_hooks"]:
        hooks = "\n".join(f"- «{h}»" for h in params["recent_hooks"])
        block += f"\n\nНе начинай так же, как эти прошлые тексты (другие слова, другая завязка):\n{hooks}"
    return block


def format_story_variety(params: dict) -> str:
    return _format_variety_block(params, [
        f"Как начать: {params['opening']}.",
        f"Тон рассказчика: {params['tone']}.",
        f"Как закончить: {params['ending']}.",
    ])


def format_drama_variety(params: dict) -> str:
    return _format_variety_block(params, [
        f"Как начать: {params['opening']}.",
        f"Тема конфликта: {params['conflict']}.",
        f"Персонаж A: {params['archetype_a']}; манера речи — {params['speech_a']}.",
        f"Персонаж B: {params['archetype_b']}; манера речи — {params['speech_b']}.",
        f"Два неожиданных поворота по ходу истории (используй оба): {params['twist_1']}; {params['twist_2']}.",
        f"Как закончить: {params['ending']}.",
    ])


def _hook(text: str) -> str:
    """Первая фраза — то, по чему легче всего заметить повтор завязки."""
    first = re.split(r"(?<=[.!?…])\s+", text.strip(), maxsplit=1)[0]
    return first[:120]


def record_story(cfg: dict, text: str, chosen: dict) -> None:
    hist = _load_history(cfg)
    lst = hist.setdefault("reddit_story", [])
    lst.append({"hook": _hook(text), "params": {k: v for k, v in chosen.items() if k != "recent_hooks"}})
    hist["reddit_story"] = lst[-HISTORY_LEN:]
    _save_history(cfg, hist)


def record_drama(cfg: dict, first_message: str, chosen: dict) -> None:
    hist = _load_history(cfg)
    lst = hist.setdefault("imessage_drama", [])
    lst.append({"hook": _hook(first_message), "params": {k: v for k, v in chosen.items() if k != "recent_hooks"}})
    hist["imessage_drama"] = lst[-HISTORY_LEN:]
    _save_history(cfg, hist)
