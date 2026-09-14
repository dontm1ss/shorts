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

# Правила стиля — в промптах владельца (prompts.STORY_SYSTEM / DRAMA_SYSTEM). Здесь — конкретные
# параметры каждого текста, выбранные случайно: модель не помнит прошлых генераций, и без этого
# «каждая новая генерация должна отличаться от предыдущих» ей нечем выполнить.

# --- режимы «иногда» из правил владельца ------------------------------------------
# На текст берутся два режима из РАЗНЫХ групп: внутри группы режимы противоречат друг другу.
MODE_GROUPS = [
    [   # эмоциональная дуга
        "первые секунды — серьёзно, потом ситуация постепенно превращается в полный пиздец",
        "начинается как комедия, а заканчивается неожиданно серьёзно",
    ],
    [   # кто виноват
        "оба персонажа — долбоёбы",
        "один персонаж очевидно неправ",
        "неправы оба",
    ],
    [   # причина конфликта
        "конфликт возникает из-за абсолютно идиотской причины",
        "причина конфликта оказывается неожиданно серьёзной",
    ],
    [   # устройство сюжета
        "зритель думает, что понял ситуацию, — и получает ещё один поворот",
        "самый незначительный персонаж оказывается главным элементом сюжета",
        "случайная деталь из самого начала становится ключом к финалу",
    ],
]
MODES = [m for group in MODE_GROUPS for m in group]

# Финал — из правил владельца; последняя реплика должна давать «блять, вот это поворот»
FINALES = [
    "максимально смешной", "абсурдный", "неожиданно логичный", "жёсткий", "нелепый", "открытый",
    "внезапно серьёзный", "такой, после которого хочется пересмотреть начало",
]

# «Меняй порядок раскрытия информации»
REVEALS = [
    "главное скрыто до последней реплики",
    "зритель с самого начала знает больше, чем один из героев",
    "правда всплывает кусками, и каждый кусок меняет картину",
    "сначала результат, потом — как до этого дошло",
    "ложный след: первые подсказки ведут совсем не туда",
    "ключевая деталь проскакивает мимоходом в начале и выстреливает в конце",
]

# «Меняй ритм»
RHYTHMS_STORY = [
    "короткие рубленые фразы",
    "длинные сбивчивые предложения, которые обрываются на полуслове",
    "спокойный рассказ с внезапными резкими фразами",
    "быстрый темп, будто рассказывает на бегу",
    "паузы и недосказанность, а потом — резкий разгон",
]
RHYTHMS_DRAMA = [
    "быстрые короткие сообщения очередями",
    "медленный разгон и резкое ускорение ближе к концу",
    "один пишет длинно, по десять-пятнадцать слов, другой отвечает односложно",
    "рваный ритм: паузы, короткие и длинные сообщения вперемешку",
    "спокойная переписка, которая взрывается одним сообщением",
]
TWIST_COUNTS = (1, 2, 3)   # «меняй количество поворотов»

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
    """Недавние значения параметра; списочные параметры (modes, twists) — развёрнутые."""
    values = []
    for h in _load_history(cfg).get(kind, [])[-limit:]:
        value = h.get("params", {}).get(key)
        values += value if isinstance(value, list) else [value] if value else []
    return values


def _pick_modes(recent: list[str], n: int = 2) -> list[str]:
    """n режимов «иногда» из разных групп (внутри группы режимы противоречат друг другу)."""
    return [_pick(group, recent)[0] for group in random.sample(MODE_GROUPS, n)]


def _common_params(cfg: dict, kind: str, rhythms: list[str]) -> dict:
    return {
        "modes": _pick_modes(_recent(cfg, kind, "modes")),
        "rhythm": _pick(rhythms, _recent(cfg, kind, "rhythm"))[0],
        "reveal": _pick(REVEALS, _recent(cfg, kind, "reveal"))[0],
        "finale": _pick(FINALES, _recent(cfg, kind, "finale"))[0],
        "recent_hooks": _recent_hooks(cfg, kind),
    }


def _recent_hooks(cfg: dict, kind: str) -> list[str]:
    return [h["hook"] for h in _load_history(cfg).get(kind, [])[-HISTORY_LEN:] if h.get("hook")]


def story_params(cfg: dict) -> dict:
    """Параметры для новой истории от первого лица + крючки последних историй."""
    return {
        "opening": _pick(OPENINGS_STORY, _recent(cfg, "reddit_story", "opening"))[0],
        "tone": _pick(TONES_STORY, _recent(cfg, "reddit_story", "tone"))[0],
        "conflict": _pick(CONFLICT_DOMAINS, _recent(cfg, "reddit_story", "conflict"))[0],
        "archetype": _pick(ARCHETYPES, _recent(cfg, "reddit_story", "archetype"))[0],
        **_common_params(cfg, "reddit_story", RHYTHMS_STORY),
    }


def drama_params(cfg: dict) -> dict:
    """Параметры для новой переписки + крючки последних переписок."""
    recent_archetypes = _recent(cfg, "imessage_drama", "archetype_a") + _recent(cfg, "imessage_drama", "archetype_b")
    recent_speech = _recent(cfg, "imessage_drama", "speech_a") + _recent(cfg, "imessage_drama", "speech_b")
    archetype_a, archetype_b = _pick(ARCHETYPES, recent_archetypes, n=2)
    speech_a, speech_b = _pick(SPEECH_MANNERS, recent_speech, n=2)
    return {
        "opening": _pick(OPENINGS_DRAMA, _recent(cfg, "imessage_drama", "opening"))[0],
        "conflict": _pick(CONFLICT_DOMAINS, _recent(cfg, "imessage_drama", "conflict"))[0],
        "archetype_a": archetype_a, "archetype_b": archetype_b,
        "speech_a": speech_a, "speech_b": speech_b,
        "twists": _pick(TWISTS, _recent(cfg, "imessage_drama", "twists"), n=random.choice(TWIST_COUNTS)),
        **_common_params(cfg, "imessage_drama", RHYTHMS_DRAMA),
    }


def _format_variety_block(params: dict, extra_lines: list[str]) -> str:
    lines = "\n".join(f"- {line}" for line in extra_lines + [
        f"Ритм: {params['rhythm']}.",
        f"Порядок раскрытия информации: {params['reveal']}.",
        f"Режимы этого текста (из «иногда»): {'; '.join(params['modes'])}.",
        f"Финал: {params['finale']}. Последняя реплика — самая сильная.",
    ])
    block = f"Параметры именно этого текста (у каждой генерации свои):\n{lines}"
    if params["recent_hooks"]:
        hooks = "\n".join(f"- «{h}»" for h in params["recent_hooks"])
        block += f"\n\nНе начинай так же, как эти прошлые тексты (другие слова, другая завязка):\n{hooks}"
    return block


def format_story_variety(params: dict, topic: str | None = None, invented: bool = False) -> str:
    """invented — историю придумывает DeepSeek (без поста): добавляются тема конфликта (если тему
    не задал пользователь) и характер второго главного героя."""
    lines = [f"Как начать: {params['opening']}.", f"Тон рассказчика: {params['tone']}."]
    if invented:
        if not topic:
            lines.append(f"Тема конфликта: {params['conflict']}.")
        lines.append(f"Второй главный персонаж: {params['archetype']}.")
    return _format_variety_block(params, lines)


def format_drama_variety(params: dict, topic: str | None = None) -> str:
    """topic — тема от пользователя: тогда случайная «тема конфликта» не подставляется, чтобы не спорить с ней."""
    twists = params["twists"]
    lines = [f"Как начать: {params['opening']}."]
    if not topic:
        lines.append(f"Тема конфликта: {params['conflict']}.")
    lines += [
        f"Персонаж A: {params['archetype_a']}; манера речи — {params['speech_a']}.",
        f"Персонаж B: {params['archetype_b']}; манера речи — {params['speech_b']}.",
        (f"Поворот по ходу истории: {twists[0]}." if len(twists) == 1 else
         f"Повороты по ходу истории ({len(twists)}, используй все): {'; '.join(twists)}."),
    ]
    return _format_variety_block(params, lines)


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
