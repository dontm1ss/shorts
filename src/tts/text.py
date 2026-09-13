"""Подготовка текста к озвучке: то, что модель прочитает плохо или не прочитает вовсе.

Промпты DeepSeek уже просят писать числа словами и без эмодзи — это страховка для постов,
поданных напрямую, и редких промахов модели. Показываемый текст (пузыри, субтитры) не меняется.
"""
from __future__ import annotations

import re

_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w])")
_PERCENT = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")
_TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# Всё, что не буквы, цифры, пробелы и обычная пунктуация: эмодзи, пиктограммы, стрелки и т.п.
_UNSPEAKABLE = re.compile(r"[^\w\s.,!?;:«»\"'()\-—–…\u0301]")


def _words(number: str, lang: str) -> str:
    from num2words import num2words
    value = float(number.replace(",", ".")) if re.search(r"[.,]", number) else int(number)
    return num2words(value, lang=lang)


def normalize_for_tts(text: str, lang: str = "ru") -> str:
    text = _TIME.sub(lambda m: f"{_words(m.group(1), lang)} {_words(m.group(2), lang)}"
                     if m.group(2) != "00" else f"{_words(m.group(1), lang)} ноль ноль", text)
    text = _PERCENT.sub(lambda m: f"{m.group(1)} процентов", text)
    text = _NUMBER.sub(lambda m: _words(m.group(1), lang), text)
    text = _UNSPEAKABLE.sub(" ", text)
    return re.sub(r"\s+([.,!?;:…])", r"\1", " ".join(text.split()))
