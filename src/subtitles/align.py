"""Пословные таймкоды: распознавание faster-whisper + выравнивание по известному тексту.

Для озвученных сцен текст известен точно (сценарий), поэтому распознаванию доверяем
только время, а слова берём из сценария. Если whisper недоступен или слова не нашлись —
время реплики (timestamp_ms / duration_ms из TTS) делится между словами пропорционально длине.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

_NON_WORD = re.compile(r"[^\w-]+")


@dataclass
class Word:
    text: str       # как показывать (из сценария, без ударений)
    start: float    # секунды от начала аудио
    end: float
    line: int = 0   # номер реплики сценария — группы субтитров не пересекают границу реплик

    def to_dict(self) -> dict:
        return asdict(self)


def save_words(words: list[Word], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([w.to_dict() for w in words], ensure_ascii=False, indent=1), encoding="utf-8")


def load_words(path: Path) -> list[Word]:
    return [Word(**w) for w in json.loads(Path(path).read_text(encoding="utf-8"))]


def clip_words(words: list[Word], start_s: float, end_s: float) -> list[Word]:
    """Слова отрезка [start_s, end_s) исходника — время от начала отрезка. Слово на границе
    берётся, если его середина внутри отрезка."""
    duration = end_s - start_s
    return [Word(w.text, max(0.0, w.start - start_s), min(duration, w.end - start_s))
            for w in words if start_s <= (w.start + w.end) / 2 < end_s]


def normalize(token: str) -> str:
    return _NON_WORD.sub("", token.lower().replace("\u0301", "").replace("ё", "е")).strip("-")


def _weights(words: list[str]) -> list[int]:
    return [len(normalize(w)) + 1 for w in words]


def proportional(words: list[str], t0: float, t1: float, line: int = 0) -> list[Word]:
    """Делит [t0, t1] между словами пропорционально их длине."""
    weights = _weights(words)
    total = sum(weights) or 1
    result, cursor = [], t0
    for word, w in zip(words, weights):
        end = cursor + (t1 - t0) * w / total
        result.append(Word(word, cursor, end, line))
        cursor = end
    return result


def align_line(words: list[str], asr: list[Word], t0: float, t1: float, line: int = 0) -> list[Word]:
    """Слова реплики из сценария + распознанные слова → слова сценария с таймкодами в [t0, t1]."""
    candidates = [w for w in asr if t0 - 0.15 <= (w.start + w.end) / 2 <= t1 + 0.15]
    if not candidates:
        return proportional(words, t0, t1, line)

    times: list[tuple[float, float] | None] = [None] * len(words)
    matcher = SequenceMatcher(None, [normalize(w) for w in words],
                              [normalize(w.text) for w in candidates], autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and i2 - i1 == j2 - j1):
            for k in range(i2 - i1):
                times[i1 + k] = (candidates[j1 + k].start, candidates[j1 + k].end)
        elif tag == "replace":   # разное число слов — делим общий отрезок распознанных
            span = proportional(words[i1:i2], candidates[j1].start, candidates[j2 - 1].end)
            for k, w in enumerate(span):
                times[i1 + k] = (w.start, w.end)

    # Пропуски (слово сценария не распознано) — между соседями
    i = 0
    while i < len(words):
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < len(words) and times[j] is None:
            j += 1
        left = times[i - 1][1] if i else t0
        right = times[j][0] if j < len(words) else t1
        for k, w in enumerate(proportional(words[i:j], left, max(left, right))):
            times[i + k] = (w.start, w.end)
        i = j

    # Монотонность и попадание в окно реплики
    result, prev_end = [], t0
    for word, (start, end) in zip(words, times):
        start = min(max(start, prev_end), t1)
        end = min(max(end, start + 0.05), t1) if start < t1 else t1
        result.append(Word(word, start, end, line))
        prev_end = end
    return result


def transcribe(audio: Path, cfg: dict, language: str | None = None,
               prompt: str | None = None) -> list[Word]:
    """faster-whisper с пословными таймкодами. language=None — автоопределение."""
    from faster_whisper import WhisperModel

    from core.cuda import ensure_cuda_dlls

    s = cfg["subtitles"]
    if s["device"] == "cuda":
        ensure_cuda_dlls()
    model = WhisperModel(s["model"], device=s["device"], compute_type=s["compute_type"],
                         cpu_threads=cfg["runtime"]["cpu_threads"])
    segments, _ = model.transcribe(str(audio), language=language, word_timestamps=True,
                                   initial_prompt=prompt, beam_size=5)
    words = [Word(w.word.strip(), w.start, w.end) for seg in segments for w in (seg.words or [])]
    del model
    return [w for w in words if w.text]


def script_words(scene: dict, asr: list[Word] | None) -> list[Word]:
    """Слова сценария озвученной сцены с таймкодами."""
    result = []
    for i, line in enumerate(scene["script"]):
        if "timestamp_ms" not in line or "duration_ms" not in line:
            raise ValueError("У реплик нет таймкодов — сначала озвучьте сцену (python -m tts scene)")
        t0 = line["timestamp_ms"] / 1000
        t1 = t0 + line["duration_ms"] / 1000
        # отдельные тире и кавычки — не слова: в титрах не показываются
        words = [w for w in line["text"].split() if any(c.isalnum() for c in w)]
        result += align_line(words, asr, t0, t1, i) if asr else proportional(words, t0, t1, i)
    return result
