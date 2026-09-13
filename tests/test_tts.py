"""TTS-модуль без настоящей модели: FakeEngine подменяет только вызов Chatterbox и stressonnx,
вся остальная логика (куски, обрезка тишины, повторы, таймлайн, голоса, запись wav) — боевая."""
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.config import ROOT
from core.scene import SceneError, scene_errors
from subtitles import Word
from tts import TTSEngine, plausible_duration, resolve_voice, split_into_chunks, synthesize_scene

SR = 16000
LETTERS_PER_S = 15


class FakeEngine(TTSEngine):
    """Речь — тон длиной «15 букв в секунду» с полусекундной тишиной по краям."""

    def __init__(self, cfg, babble_attempts=0):
        super().__init__(cfg)
        self.calls: list[tuple[str, Path | None]] = []
        self.seeds: list[int] = []
        self.babble_attempts = babble_attempts   # столько первых вызовов «бормочут» (×10 длиннее)

    @property
    def sr(self):
        return SR

    def stress(self, text):
        return text.replace("о", "о\u0301", 1)

    def _seed(self, seed):
        self.seeds.append(seed)

    def _raw_generate(self, text, params, voice):
        self.calls.append((text, voice))
        speech_s = sum(c.isalpha() for c in text) / LETTERS_PER_S
        if len(self.calls) <= self.babble_attempts:
            speech_s *= 10
        tone = 0.5 * np.sin(np.arange(int(speech_s * SR)) * 0.3)
        silence = np.zeros(SR // 2)
        return np.concatenate([silence, tone, silence]).astype(np.float32)


def mock_scene(name):
    return json.loads((ROOT / "mock-data" / f"scene.{name}.json").read_text(encoding="utf-8"))


# --- куски текста -----------------------------------------------------------------

def test_short_text_is_one_chunk():
    assert split_into_chunks("Раз.  Два!\nТри?", 100) == ["Раз. Два! Три?"]


def test_sentences_packed_under_limit():
    sentence = "Это предложение нужно только для того, чтобы проверить упаковку."
    text = " ".join([sentence] * 6)
    chunks = split_into_chunks(text, 140)
    assert len(chunks) == 3
    assert all(len(c) <= 140 and c.endswith(".") for c in chunks)
    assert " ".join(chunks) == text


def test_long_sentence_split_by_clauses_then_words():
    long_sentence = ", ".join(["часть длинного предложения без точки"] * 10) + "."
    no_punct = " ".join(["слово"] * 100)
    for text in (long_sentence, no_punct):
        chunks = split_into_chunks(text, 80)
        assert all(0 < len(c) <= 80 for c in chunks)
        assert " ".join(chunks) == text


# --- проверка длительности ------------------------------------------------------

@pytest.mark.parametrize("text, seconds, ok", [
    ("Никто в деревне не знал", 1.6, True),
    ("Никто в деревне не знал", 12.0, False),   # бормотание / повтор
    ("Никто в деревне не знал", 0.2, False),    # обрыв
    ("Да.", 0.9, True),                          # короткая реплика — запас в секунду
    ("стари\u0301нный", 0.7, True),              # ударение не считается буквой
    ("...", 0.5, True),
])
def test_plausible_duration(text, seconds, ok):
    assert plausible_duration(text, seconds) is ok


# --- синтез ---------------------------------------------------------------------

def test_silence_trimmed(dev_cfg):
    engine = FakeEngine(dev_cfg)
    audio, stressed = engine.synthesize("Старинный замок стоял на холме.", {"exaggeration": 0.5})
    speech_s = sum(c.isalpha() for c in "Старинный замок стоял на холме.") / LETTERS_PER_S
    assert stressed.count("\u0301") == 1
    assert len(audio) / SR < speech_s + 0.3          # из 1 с тишины осталось < 0.3 с


def test_babble_is_regenerated(dev_cfg):
    engine = FakeEngine(dev_cfg, babble_attempts=1)
    audio, _ = engine.synthesize("Никто в деревне не знал.", {"exaggeration": 0.5}, seed=7)
    assert len(engine.calls) == 2
    assert engine.seeds == [7, 8]                     # повтор — с другим seed
    assert plausible_duration("Никто в деревне не знал.", len(audio) / SR)


def test_gives_up_after_max_retries(dev_cfg):
    engine = FakeEngine(dev_cfg, babble_attempts=99)
    engine.synthesize("Никто в деревне не знал.", {"exaggeration": 0.5})
    assert len(engine.calls) == dev_cfg["tts"]["max_retries"] + 1


def test_junk_tail_cut_by_recognized_words(dev_cfg):
    """Chatterbox дописывает после речи громкий шум — обрезка по громкости его не убирает."""
    text = "Ты правда думала, что я не узнаю?"
    speech_s = sum(c.isalpha() for c in text) / LETTERS_PER_S
    engine = FakeEngine(dev_cfg, junk_tail_s=2.5)
    audio, _ = engine.synthesize(text, {"exaggeration": 0.5})
    assert len(audio) / SR < speech_s + 0.4                       # 2.5 с хвоста срезаны

    dev_cfg["tts"]["verify"] = False
    audio, _ = FakeEngine(dev_cfg, junk_tail_s=2.5).synthesize(text, {"exaggeration": 0.5})
    assert len(audio) / SR > speech_s + 2.5                       # без проверки хвост остаётся


def test_garbled_speech_regenerated(dev_cfg):
    engine = FakeEngine(dev_cfg, garble_attempts=1)
    engine.synthesize("Никто в деревне не знал.", {"exaggeration": 0.5})
    assert len(engine.calls) == 2


def test_text_error():
    from tts.engine import text_error
    assert text_error("Никто не знал.", [Word("никто", 0, 1), Word("не", 1, 2), Word("знал", 2, 3)]) == 0
    assert 0 < text_error("Мой сосед ставил машину", [Word("мой", 0, 1), Word("сосед", 1, 2),
                                                      Word("втягил", 2, 3), Word("машину", 3, 4)]) < 0.25
    assert text_error("Никто не знал.", [Word("бла", 0, 1)]) > 0.5


def test_scene_timeline(dev_cfg, tmp_path):
    scene = mock_scene("imessage_drama")
    engine = FakeEngine(dev_cfg)
    out = tmp_path / "drama.wav"
    result = synthesize_scene(scene, engine, out_path=out)

    assert scene_errors(result) == []
    assert "stressed_text" not in scene["script"][0]            # исходная сцена не тронута
    gap_ms = dev_cfg["tts"]["pause_between_lines_ms"]
    expected_start = 0
    for line in result["script"]:
        assert line["timestamp_ms"] == pytest.approx(expected_start, abs=1)
        assert line["duration_ms"] > 0
        assert "\u0301" in line["stressed_text"] or "о" not in line["text"]
        expected_start = line["timestamp_ms"] + line["duration_ms"] + gap_ms

    data, sr = sf.read(out)
    assert sr == SR
    last = result["script"][-1]
    assert result["audio"]["duration_ms"] == pytest.approx(len(data) / SR * 1000, abs=1)
    assert result["audio"]["duration_ms"] == pytest.approx(last["timestamp_ms"] + last["duration_ms"], abs=2)
    assert result["audio"]["tts_params"] == dev_cfg["tts"]["presets"][dev_cfg["tts"]["scene_presets"]["imessage_drama"]]


def test_voices_per_role(dev_cfg, tmp_path):
    voice_a = tmp_path / "a.wav"
    sf.write(voice_a, np.zeros(SR), SR)
    dev_cfg["tts"]["voices"]["A"] = str(voice_a)
    engine = FakeEngine(dev_cfg)
    synthesize_scene(mock_scene("imessage_drama"), engine, preset="calm", out_path=tmp_path / "o.wav")
    voices = [v for _, v in engine.calls]
    assert voices == [voice_a, None, voice_a, None]             # A, B, A, B


def test_voice_resolution(tmp_path):
    assert resolve_voice("narrator", {"narrator": ""}) is None
    assert resolve_voice("C", {}) is None
    with pytest.raises(FileNotFoundError, match="'A'"):
        resolve_voice("A", {"A": "voices/nope.wav"})


def test_scene_without_script(dev_cfg, tmp_path):
    with pytest.raises(SceneError, match="нет script"):
        synthesize_scene(mock_scene("video_clip"), FakeEngine(dev_cfg), out_path=tmp_path / "x.wav")


def test_unknown_preset(dev_cfg, tmp_path):
    with pytest.raises(ValueError, match="Неизвестный пресет"):
        synthesize_scene(mock_scene("reddit_story"), FakeEngine(dev_cfg), preset="loud",
                         out_path=tmp_path / "x.wav")
