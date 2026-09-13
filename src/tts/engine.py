"""Обёртка над Chatterbox Multilingual: ударения, пресеты, голоса, синтез по кускам.

Пайплайн (docs/tts-stack-spec.md, раздел 10):
    текст → stressonnx (U+0301) → куски по предложениям → model.generate → обрезка тишины
    → проверка whisper'ом: обрезка по последнему слову + повтор при плохом распознавании
    → проверка длительности → склейка с паузами
"""
from __future__ import annotations

import gc
import logging
import re
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np

from subtitles.align import Word, normalize

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_CLAUSE_END = re.compile(r"(?<=[,;:—–-])\s+")
_LETTER = re.compile(r"[^\W\d_]")   # U+0301 — не буква (категория Mn), в счёт не идёт


def _pack(parts: list[str], max_chars: int) -> list[str]:
    chunks, current = [], ""
    for part in parts:
        if current and len(current) + 1 + len(part) > max_chars:
            chunks.append(current)
            current = part
        else:
            current = f"{current} {part}" if current else part
    if current:
        chunks.append(current)
    return chunks


def split_into_chunks(text: str, max_chars: int) -> list[str]:
    """Режет текст по предложениям и склеивает их в куски не длиннее max_chars.
    Предложение длиннее лимита режется по знакам препинания внутри, в крайнем случае — по словам."""
    pieces = []
    for sentence in _SENTENCE_END.split(" ".join(text.split())):
        if len(sentence) <= max_chars:
            pieces.append(sentence)
            continue
        for clause in _CLAUSE_END.split(sentence):
            pieces.extend([clause] if len(clause) <= max_chars else _pack(clause.split(), max_chars))
    return _pack([p for p in pieces if p], max_chars)


def text_error(expected: str, words: list[Word]) -> float:
    """Доля несовпадения букв заданного и распознанного текста (0 — совпало, 1 — ничего общего)."""
    want = "".join(normalize(w) for w in expected.split())
    got = "".join(normalize(w.text) for w in words)
    if not want:
        return 0.0
    return 1.0 - SequenceMatcher(None, want, got, autojunk=False).ratio()


def plausible_duration(text: str, seconds: float) -> bool:
    """Грубая проверка на «бормотание» и обрывы. Русская речь — примерно 10–20 букв в секунду;
    допускаем 5–30 плюс секунду запаса для очень коротких реплик."""
    letters = len(_LETTER.findall(text))
    if letters == 0:
        return True
    return letters / 30 <= seconds <= letters / 5 + 1.0


class TTSEngine:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.tts_cfg = cfg["tts"]
        self.device = self.tts_cfg["device"]
        self._model = None
        self._whisper = None
        self._voices: dict[str | None, object] = {}

    @property
    def model(self):
        if self._model is None:
            import torch
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS

            # Ударения ставит stressonnx; встроенный ударятор Chatterbox не установлен
            # и на каждый вызов пишет об этом предупреждение.
            logging.getLogger("chatterbox.models.tokenizers.tokenizer").setLevel(logging.ERROR)
            if self.device == "cuda" and not torch.cuda.is_available():
                raise RuntimeError("Профиль требует CUDA, но torch.cuda.is_available() = False")
            log.info("Загрузка Chatterbox Multilingual на %s…", self.device)
            self._model = ChatterboxMultilingualTTS.from_pretrained(device=self.device)
            self._voices[None] = self._model.conds   # встроенный голос
        return self._model

    @property
    def sr(self) -> int:
        return self.model.sr

    def unload(self) -> None:
        """Освободить память (VRAM делится с whisper на целевом железе)."""
        if self._model is None and self._whisper is None:
            return
        self._model = None
        self._whisper = None
        self._voices.clear()
        gc.collect()
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def stress(self, text: str) -> str:
        from stressonnx import stress
        return stress(text, self.tts_cfg["language_id"])

    def _seed(self, seed: int) -> None:
        import torch
        torch.manual_seed(seed)

    def _use_voice(self, voice: Path | None, exaggeration: float) -> None:
        model = self.model
        key = str(voice) if voice else None
        if key not in self._voices:
            model.prepare_conditionals(key, exaggeration=exaggeration)
            self._voices[key] = model.conds
        model.conds = self._voices[key]

    def _raw_generate(self, text: str, params: dict, voice: Path | None) -> np.ndarray:
        """Один вызов модели → моно float32 без обработки."""
        self._use_voice(voice, params["exaggeration"])
        wav = self.model.generate(text, language_id=self.tts_cfg["language_id"], **params)
        return wav.squeeze(0).cpu().numpy().astype(np.float32)

    def _trim(self, wav: np.ndarray) -> np.ndarray:
        import librosa
        if wav.size == 0:
            return wav
        _, (start, end) = librosa.effects.trim(wav, top_db=self.tts_cfg["trim_top_db"])
        margin = int(0.03 * self.sr)   # не срезать хвосты согласных
        return wav[max(0, start - margin):min(len(wav), end + margin)]

    def _recognize(self, wav: np.ndarray) -> list[Word]:
        """Пословное распознавание куска (whisper держится в памяти вместе с TTS)."""
        import librosa
        from faster_whisper import WhisperModel

        if self._whisper is None:
            s = self.cfg["subtitles"]
            self._whisper = WhisperModel(s["model"], device=s["device"], compute_type=s["compute_type"],
                                         cpu_threads=self.cfg["runtime"]["cpu_threads"])
        audio16 = librosa.resample(wav, orig_sr=self.sr, target_sr=16000)
        segments, _ = self._whisper.transcribe(audio16, language=self.tts_cfg["language_id"],
                                               word_timestamps=True, beam_size=1,
                                               condition_on_previous_text=False)
        return [Word(w.word.strip(), w.start, w.end)
                for seg in segments for w in (seg.words or []) if w.word.strip()]

    def _verify(self, wav: np.ndarray, text: str) -> tuple[np.ndarray, float]:
        """Обрезка по распознанной речи + доля ошибки. Хвостовой шум/бормотание уходит."""
        words = self._recognize(wav)
        if not words:
            return wav, 1.0
        start = max(0.0, words[0].start - 0.08)
        end = min(len(wav) / self.sr, words[-1].end + 0.25)
        return wav[int(start * self.sr):int(end * self.sr)], text_error(text, words)

    def _generate_checked(self, text: str, params: dict, voice: Path | None, seed: int) -> np.ndarray:
        attempts = self.tts_cfg["max_retries"] + 1
        best: tuple[float, np.ndarray] | None = None
        for attempt in range(attempts):
            self._seed(seed + attempt)
            wav = self._trim(self._raw_generate(text, params, voice))
            error = 0.0
            if self.tts_cfg["verify"]:
                wav, error = self._verify(wav, text)
            seconds = len(wav) / self.sr
            plausible = plausible_duration(text, seconds)
            if plausible and error <= self.tts_cfg["max_text_error"]:
                return wav
            log.warning("Кусок не прошёл проверку (ошибка распознавания %.0f%%, %.1f с%s) — попытка %d/%d: %r",
                        error * 100, seconds, "" if plausible else ", неправдоподобная длина",
                        attempt + 1, attempts, text[:60])
            score = error + (0 if plausible else 1)
            if best is None or score < best[0]:
                best = (score, wav)
        return best[1]

    def synthesize(self, text: str, params: dict, voice: Path | None = None,
                   seed: int | None = None) -> tuple[np.ndarray, str]:
        """Текст → (моно float32 с частотой self.sr, текст с ударениями)."""
        seed = self.tts_cfg["seed"] if seed is None else seed
        stressed = self.stress(text)
        parts = []
        for i, chunk in enumerate(split_into_chunks(stressed, self.tts_cfg["max_chars_per_chunk"])):
            if parts:
                parts.append(np.zeros(int(self.tts_cfg["pause_between_chunks_ms"] / 1000 * self.sr), np.float32))
            parts.append(self._generate_checked(chunk, params, voice, seed + 100 * i))
        return (np.concatenate(parts) if parts else np.zeros(0, np.float32)), stressed
