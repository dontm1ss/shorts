"""TTS-модуль без настоящей модели: FakeEngine подменяет только вызов Chatterbox и stressonnx,
вся остальная логика (куски, обрезка тишины, повторы, таймлайн, голоса, запись wav) — боевая."""
import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from core.scene import SceneError, scene_errors
from subtitles import Word
from tts import TTSEngine, plausible_duration, resolve_voice, split_into_chunks, synthesize_scene

SR = 16000
LETTERS_PER_S = 15


class FakeEngine(TTSEngine):
    """Речь — тон длиной «15 букв в секунду» с полусекундной тишиной по краям.
    «Whisper» распознаёт слова текста, равномерно разложенные по речи."""

    def __init__(self, cfg, babble_attempts=0, junk_tail_s=0.0, garble_attempts=0):
        super().__init__(cfg)
        self.calls: list[tuple[str, Path | None]] = []
        self.params: list[dict] = []
        self.seeds: list[int] = []
        self.babble_attempts = babble_attempts   # столько первых вызовов «бормочут» (×10 длиннее)
        self.garble_attempts = garble_attempts   # столько первых вызовов распознаются как мусор
        self.junk_tail_s = junk_tail_s           # громкий шум после речи — как у Chatterbox
        self._speech_s = 0.0
        self._text = ""

    @property
    def sr(self):
        return SR

    def stress(self, text):
        return text.replace("о", "о\u0301", 1)

    def _seed(self, seed):
        self.seeds.append(seed)

    def _raw_generate(self, text, params, voice):
        self.calls.append((text, voice))
        self.params.append(dict(params))
        self._text = text
        speech_s = sum(c.isalpha() for c in text) / LETTERS_PER_S
        if len(self.calls) <= self.babble_attempts:
            speech_s *= 10
        self._speech_s = speech_s
        tone = 0.5 * np.sin(np.arange(int(speech_s * SR)) * 0.3)
        junk = 0.4 * np.sin(np.arange(int(self.junk_tail_s * SR)) * 0.7)
        silence = np.zeros(SR // 2)
        return np.concatenate([silence, tone, np.zeros(SR // 4), junk, silence]).astype(np.float32)

    def _recognize(self, wav):
        start = 0.03                        # после librosa-обрезки речь начинается через ~30 мс поля
        if len(self.calls) <= self.garble_attempts:
            return [Word("бла", start, start + self._speech_s)]
        words = self._text.split()
        step = self._speech_s / len(words)
        return [Word(w, start + i * step, start + (i + 1) * step) for i, w in enumerate(words)]


def mock_scene(name):
    return json.loads((Path(__file__).parent / "fixtures" / f"scene.{name}.json").read_text(encoding="utf-8"))


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


def test_emoji_only_message_is_silent_pause(dev_cfg, tmp_path):
    """Сообщение из одних эмодзи: пузырь есть, голоса нет — пауза silent_line_ms, модель не вызывается."""
    scene = mock_scene("imessage_drama")
    scene["script"][1]["text"] = "💀💀"
    scene["script"][2]["text"] = "Скрин уже у всей группы 🙃"
    engine = FakeEngine(dev_cfg)
    result = synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    assert [text for text, _ in engine.calls][1].startswith("Скрин") and len(engine.calls) == 3
    assert "🙃" not in engine.calls[1][0]                                        # эмодзи озвучка не читает
    silent = result["script"][1]
    assert silent["duration_ms"] == dev_cfg["tts"]["silent_line_ms"] and silent["stressed_text"] == "💀💀"
    assert result["script"][2]["timestamp_ms"] == pytest.approx(
        silent["timestamp_ms"] + silent["duration_ms"] + dev_cfg["tts"]["pause_between_lines_ms"], abs=1)


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

    female, role_b = tmp_path / "f.wav", tmp_path / "b.wav"
    for path in (female, role_b):
        sf.write(path, np.zeros(SR), SR)
    voices = {"female": str(female), "male": "", "A": "", "B": str(role_b)}
    assert resolve_voice("A", voices, "female") == female        # по полу героя
    assert resolve_voice("B", voices, "female") == role_b        # явный голос роли важнее пола
    assert resolve_voice("A", voices, "male") is None            # мужской не задан — встроенный
    assert resolve_voice("A", voices, None) is None


def test_voices_by_gender(dev_cfg, tmp_path):
    female, male = tmp_path / "f.wav", tmp_path / "m.wav"
    for path in (female, male):
        sf.write(path, np.zeros(SR), SR)
    dev_cfg["tts"]["voices"].update(female=str(female), male=str(male))
    scene = mock_scene("imessage_drama")
    scene["meta"]["genders"] = {"A": "male", "B": "female"}
    engine = FakeEngine(dev_cfg)
    synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    assert [v for _, v in engine.calls] == [male, female, male, female]   # A, B, A, B


def _records(folder, *names):
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        sf.write(folder / name, np.zeros(SR), SR, format="WAV" if name.endswith(".wav") else "FLAC")
    return folder


def _peak_hz(path):
    wav, sr = sf.read(path)
    wav = wav if wav.ndim == 1 else wav.mean(axis=1)
    spectrum = np.abs(np.fft.rfft(wav * np.hanning(len(wav))))
    return np.fft.rfftfreq(len(wav), 1 / sr)[spectrum.argmax()]


def test_same_gender_roles_get_distinct_voices(dev_cfg, tmp_path):
    """Двое мужчин в переписке: первый заговоривший — со своими записями, второй — с теми же, но
    сдвинутыми по высоте (или со вторым набором записей, если он есть)."""
    male = tmp_path / "male"
    male.mkdir()
    tone = 0.3 * np.sin(2 * np.pi * 220 * np.arange(2 * SR) / SR)
    for name in ("neutral.wav", "anger.wav"):
        sf.write(male / name, tone.astype(np.float32), SR)
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    dev_cfg["tts"]["voices"].update(male=str(male))
    scene = mock_scene("imessage_drama")                          # A, B, A, B
    scene["meta"]["genders"] = {"A": "male", "B": "male"}
    scene["script"][1]["emotion"] = "anger"
    engine = FakeEngine(dev_cfg)
    synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    voices = [v for _, v in engine.calls]
    assert voices[0] == voices[2] == male / "neutral.wav"         # A заговорил первым — свой голос
    shifted = voices[1]
    assert shifted.parent == tmp_path / "out" / "cache" / "voices" and shifted.stem.startswith("anger_+2.5st")
    assert _peak_hz(shifted) == pytest.approx(220 * 2 ** (2.5 / 12), rel=0.02)     # выше на 2.5 полутона
    assert voices[3] != voices[1] and voices[3].stem.startswith("neutral_+2.5st")   # у каждой эмоции своя копия

    engine = FakeEngine(dev_cfg)                                   # второй раз — из кэша, тот же файл
    synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    assert [v for _, v in engine.calls] == voices

    second = _records(tmp_path / "male2", "neutral.wav")          # есть второй набор записей — без сдвига
    dev_cfg["tts"]["voices"]["male_2"] = str(second)
    engine = FakeEngine(dev_cfg)
    synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    assert [v for _, v in engine.calls] == [male / "neutral.wav", second / "neutral.wav"] * 2


def test_shift_without_rubberband(tmp_path, monkeypatch):
    """ffmpeg без rubberband (Mac): та же высота через частоту дискретизации, темп прежний."""
    import tts.scene as scene_mod
    real = scene_mod.run_ffmpeg

    def no_rubberband(args, **kw):
        if "rubberband" in args[args.index("-af") + 1]:
            raise scene_mod.MediaError("No such filter: 'rubberband'")
        return real(args, **kw)

    monkeypatch.setattr(scene_mod, "run_ffmpeg", no_rubberband)
    src = tmp_path / "neutral.wav"
    sf.write(src, (0.3 * np.sin(2 * np.pi * 220 * np.arange(2 * SR) / SR)).astype(np.float32), SR)
    out = scene_mod.shifted_reference(src, 3, tmp_path / "cache")
    assert _peak_hz(out) == pytest.approx(220 * 2 ** (3 / 12), rel=0.02)
    assert sf.info(out).duration == pytest.approx(2.0, abs=0.05)


def test_distinct_voices_only_when_shared(dev_cfg, tmp_path):
    from tts.scene import second_voices
    male, female = _records(tmp_path / "male", "neutral.wav"), _records(tmp_path / "female", "neutral.wav")
    voices = {"male": str(male), "female": str(female), "male_2": str(tmp_path / "нет"), "A": "", "B": ""}
    script = [{"speaker": s} for s in "BAB"]
    shifts = dev_cfg["tts"]["second_voice"]
    assert second_voices(script, voices, {"A": "male", "B": "female"}, shifts)[1] == {}     # разный пол
    same = second_voices(script, voices, {"A": "male", "B": "male"}, shifts)
    assert same[1] == {"A": shifts["male"]} and same[0]["A"] == ""       # B первым — A сдвиг; папки male_2 нет
    assert second_voices(script, {**voices, "A": str(female / "neutral.wav")},             # у роли свой голос
                         {"A": "male", "B": "male"}, shifts)[1] == {}
    assert second_voices(script, voices, {"A": "male", "B": "male"}, {"male": 0})[1] == {}  # сдвиг выключен
    three = [{"speaker": s} for s in "ABC"]                        # третьей — сдвиг в другую сторону
    assert second_voices(three, voices, dict.fromkeys("ABC", "female"), shifts)[1] == {"B": shifts["female"],
                                                                                        "C": -shifts["female"]}


def test_emotion_reference_choice(tmp_path):
    """Запись эмоции реплики → neutral → любая записанная эмоция → встроенный голос."""
    from tts.scene import find_reference
    full = _records(tmp_path / "male", "neutral.wav", "anger.flac")
    voices = {"male": str(full), "female": str(tmp_path / "female"), "A": ""}
    assert resolve_voice("A", voices, "male", "anger") == full / "anger.flac"      # любой формат с тем же именем
    assert resolve_voice("A", voices, "male", "sad") == full / "neutral.wav"       # нет записи — neutral
    assert resolve_voice("A", voices, "male") == full / "neutral.wav"              # без эмоции — neutral
    only_joy = _records(tmp_path / "joy_only", "joy.wav")
    assert find_reference(only_joy, "sad") == only_joy / "joy.wav"                 # тембр важнее эмоции
    assert resolve_voice("A", voices, "female", "anger") is None                   # папки нет — встроенный
    assert find_reference(_records(tmp_path / "empty"), "anger") is None


def test_emotion_params(dev_cfg):
    from tts.scene import emotion_params
    base = {"exaggeration": 0.5, "cfg_weight": 0.5, "temperature": 0.8}
    shifts = dev_cfg["tts"]["emotions"]
    assert emotion_params(base, shifts, "neutral") == base
    assert emotion_params(base, shifts, None) == base
    assert emotion_params(base, shifts, "anger")["exaggeration"] == 0.65
    assert emotion_params(base, shifts, "tired") == {"exaggeration": 0.35, "cfg_weight": 0.4, "temperature": 0.8}
    assert emotion_params(base, {"sad": {"exaggeration": -2}}, "sad")["exaggeration"] == 0.0   # не ниже нуля
    with pytest.raises(ValueError, match="speed"):
        emotion_params(base, {"joy": {"speed": 1.2}}, "joy")


def test_scene_voiced_by_emotion(dev_cfg, tmp_path, caplog):
    """Реплика с эмоцией → запись этой эмоции для пола героя + поправка подачи."""
    female = _records(tmp_path / "female", "neutral.wav", "anger.wav", "shock.wav")
    male = _records(tmp_path / "male", "neutral.wav")
    dev_cfg["tts"]["voices"].update(female=str(female), male=str(male))
    scene = mock_scene("imessage_drama")
    scene["meta"]["genders"] = {"A": "female", "B": "male"}
    for line, emotion in zip(scene["script"], ["anger", "shock", None, "guilty"]):
        if emotion:
            line["emotion"] = emotion
    engine = FakeEngine(dev_cfg)
    with caplog.at_level("WARNING"):
        result = synthesize_scene(scene, engine, out_path=tmp_path / "o.wav")
    assert scene_errors(result) == []
    assert [v for _, v in engine.calls] == [female / "anger.wav", male / "neutral.wav",
                                            female / "neutral.wav", male / "neutral.wav"]
    base = dev_cfg["tts"]["presets"]["neutral"]
    assert [p["exaggeration"] for p in engine.params] == [0.65, 0.65, base["exaggeration"], base["exaggeration"]]
    assert "shock.wav, guilty.wav" not in caplog.text and "guilty.wav, shock.wav" in caplog.text   # у мужского нет
    assert result["audio"]["tts_params"] == base                     # в сцене — параметры пресета


def test_reference_audio_trimmed_and_leveled(dev_cfg, tmp_path):
    """Тишина в начале записи съела бы первые 6 с, по которым модель берёт интонацию."""
    import pyloudnorm
    t = np.arange(2 * SR) / SR
    speech = 0.05 * np.sin(2 * np.pi * 220 * t) * (1 + 0.5 * np.sin(2 * np.pi * 3 * t))
    path = tmp_path / "neutral.wav"
    sf.write(path, np.concatenate([np.zeros(int(1.5 * SR)), speech, np.zeros(SR)]).astype(np.float32), SR)
    wav, sr = sf.read(TTSEngine(dev_cfg).reference_audio(path))
    assert sr == SR and len(wav) / SR == pytest.approx(2.1, abs=0.15)          # 2 с речи + поля по 0.05 с
    assert pyloudnorm.Meter(SR).integrated_loudness(wav) == pytest.approx(-27, abs=0.5)


def test_scene_without_script(dev_cfg, tmp_path):
    with pytest.raises(SceneError, match="нет script"):
        synthesize_scene(mock_scene("video_clip"), FakeEngine(dev_cfg), out_path=tmp_path / "x.wav")


def test_unknown_preset(dev_cfg, tmp_path):
    with pytest.raises(ValueError, match="Неизвестный пресет"):
        synthesize_scene(mock_scene("reddit_story"), FakeEngine(dev_cfg), preset="loud",
                         out_path=tmp_path / "x.wav")


@pytest.mark.parametrize("raw, spoken", [
    ("Мне 3 года.", "Мне три года."),
    ("Встретимся в 21:30!", "Встретимся в двадцать один тридцать!"),
    ("Скидка 50% на всё", "Скидка пятьдесят процентов на всё"),
    ("Poof! Ты свободна 😑", "Poof! Ты свободна"),
    ("Без цифр — как есть…", "Без цифр — как есть…"),
])
def test_normalize_for_tts(raw, spoken):
    from tts.text import normalize_for_tts
    assert normalize_for_tts(raw) == spoken


def test_token_cap_passed_to_model_and_restored(dev_cfg):
    """generate() Chatterbox зашивает max_new_tokens=1000 — движок подменяет его по длине текста."""
    import torch

    class T3:
        def inference(self, **kw):
            self.seen = kw["max_new_tokens"]

    class Model:
        sr = SR
        t3 = T3()

        def generate(self, text, language_id, **params):
            self.t3.inference(max_new_tokens=1000)
            return torch.zeros(1, SR)

    engine = TTSEngine(dev_cfg)
    engine._model = Model()
    engine._voices[None] = None
    text = "Никто в деревне не знал, кто живёт в замке."
    engine._raw_generate(text, {"exaggeration": 0.5}, None)
    assert engine._model.t3.seen == TTSEngine.token_cap(text) < 1000
    assert "inference" not in vars(engine._model.t3)          # подмена снята
    assert TTSEngine.token_cap("а" * 5000) == 1000 and TTSEngine.token_cap("Да.") == 100
