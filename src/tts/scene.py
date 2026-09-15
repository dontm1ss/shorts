"""Озвучка сцены: реплики по ролям → один wav + таймкоды реплик + блок audio."""
from __future__ import annotations

import copy
import hashlib
import logging
import os
import re
from pathlib import Path

import numpy as np
import soundfile as sf

from core.config import ROOT
from core.emotions import DEFAULT as DEFAULT_EMOTION, EMOTIONS
from core.media import MediaError, run_ffmpeg
from core.scene import SceneError, output_dir, rel_path, validate_scene, work_dir
from subtitles.align import Word, load_words, save_words
from tts.engine import TTSEngine
from tts.text import normalize_for_tts

log = logging.getLogger(__name__)

# Форматы записей-референсов (читает librosa; m4a — через ffmpeg)
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".ogg", ".m4a")
_PARAM_KEYS = ("exaggeration", "cfg_weight", "temperature")
_SPEAKABLE = re.compile(r"[^\W_]")   # буква или цифра — есть что озвучивать (не одни эмодзи)


def voice_source(speaker: str, voices: dict, gender: str | None = None) -> Path | None:
    """Голос роли из tts.voices (файл или папка с записями по эмоциям): явный голос роли →
    голос по полу героя (female / male) → None — встроенный голос Chatterbox."""
    value = voices.get(speaker) or (voices.get(gender) if gender else "")
    if not value:
        return None
    path = Path(value) if Path(value).is_absolute() else ROOT / value
    if path.suffix.lower() in AUDIO_EXTS and not path.exists():
        raise FileNotFoundError(f"Референс-голос для роли {speaker!r} не найден: {path}")
    return path


def find_reference(folder: Path, emotion: str) -> Path | None:
    """Запись <эмоция>.* в папке голоса → neutral → любая записанная эмоция → None."""
    for name in (emotion, DEFAULT_EMOTION, *EMOTIONS):
        for ext in AUDIO_EXTS:
            if (path := folder / f"{name}{ext}").is_file():
                return path
    return None


def resolve_voice(speaker: str, voices: dict, gender: str | None = None,
                  emotion: str | None = None) -> Path | None:
    """Референс реплики: голос роли/пола, из папки — запись с эмоцией реплики.
    None — встроенный голос Chatterbox (голос не задан или записей ещё нет)."""
    source = voice_source(speaker, voices, gender)
    if source is None or source.is_file():
        return source
    return find_reference(source, emotion or DEFAULT_EMOTION) if source.is_dir() else None


def second_voices(script: list[dict], voices: dict, genders: dict, shifts: dict) -> tuple[dict, dict]:
    """Два героя с одним источником голоса (одного пола — общая папка записей) звучали бы одинаково.
    Роль, заговорившая первой, остаётся со своим голосом, следующая получает второй набор записей
    пола (<пол>_2 в tts.voices), а без него — те же записи со сдвигом высоты (tts.second_voice;
    третья такая роль — со сдвигом в другую сторону). Возвращает (tts.voices с голосами ролей
    из второго набора, {роль: сдвиг в полутонах})."""
    role_voices, role_shifts, taken = dict(voices), {}, {}
    for speaker in dict.fromkeys(line["speaker"] for line in script):   # в порядке первой реплики
        gender = genders.get(speaker)
        source = voice_source(speaker, voices, gender)
        if source is None:
            continue
        before = taken.setdefault(source, [])
        before.append(speaker)
        if len(before) == 1:
            continue
        second = voice_source(speaker, {speaker: voices.get(f"{gender}_2", "")}) if gender else None
        if len(before) == 2 and second is not None and (second.is_file() or second.is_dir()
                                                        and find_reference(second, DEFAULT_EMOTION)):
            role_voices[speaker] = str(second)
        elif shift := shifts.get(gender or "", 0):
            role_shifts[speaker] = shift if len(before) == 2 else -shift
    return role_voices, role_shifts


def shifted_reference(path: Path, semitones: float, cache_dir: Path) -> Path:
    """Копия записи-референса, сдвинутая по высоте вместе с формантами: голос другого «размера»,
    а не тот же человек выше или ниже. Модель клонирует голос уже из неё, поэтому звучит естественно.
    Кэш — по пути, дате изменения записи и сдвигу."""
    key = hashlib.sha1(f"{path.resolve()}|{path.stat().st_mtime_ns}|{semitones}".encode()).hexdigest()[:10]
    out = cache_dir / f"{path.stem}_{semitones:+g}st_{key}.wav"
    if out.exists():
        return out
    cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp.wav")
    ratio = 2 ** (semitones / 12)
    try:
        run_ffmpeg(["-i", str(path), "-af", f"rubberband=pitch={ratio:.6f}:pitchq=quality", str(tmp)])
    except MediaError:   # ffmpeg без rubberband (Mac): та же высота через частоту дискретизации
        run_ffmpeg(["-i", str(path), "-af", f"aresample=48000,asetrate={48000 * ratio:.0f},"
                                            f"aresample=48000,atempo={1 / ratio:.6f}", str(tmp)])
    os.replace(tmp, out)
    return out


def emotion_params(params: dict, emotions: dict, emotion: str | None) -> dict:
    """Параметры пресета + поправка эмоции (tts.emotions)."""
    shift = emotions.get(emotion or DEFAULT_EMOTION, {})
    if unknown := set(shift) - set(_PARAM_KEYS):
        raise ValueError(f"tts.emotions.{emotion}: неизвестные параметры {', '.join(sorted(unknown))}")
    result = dict(params)
    for key, delta in shift.items():
        value = result.get(key, 0.0) + delta
        result[key] = round(max(0.05 if key == "temperature" else 0.0, value), 3)
    return result


def default_audio_path(cfg: dict, scene: dict) -> Path:
    return work_dir(cfg, scene) / "voice.wav"


def synthesize_scene(scene: dict, engine: TTSEngine, preset: str | None = None,
                     out_path: Path | None = None, seed: int | None = None) -> dict:
    """Возвращает копию сцены с заполненными stressed_text / timestamp_ms / duration_ms
    у реплик и блоком audio. Сама сцена не меняется."""
    if not scene.get("script"):
        raise SceneError("В сцене нет script — нечего озвучивать")
    tts_cfg = engine.tts_cfg
    preset = preset or tts_cfg["scene_presets"].get(scene["type"], "neutral")
    if preset not in tts_cfg["presets"]:
        raise ValueError(f"Неизвестный пресет {preset!r}. Доступны: {', '.join(tts_cfg['presets'])}")
    params = dict(tts_cfg["presets"][preset])
    seed = tts_cfg["seed"] if seed is None else seed
    out_path = Path(out_path or default_audio_path(engine.cfg, scene))
    scene = copy.deepcopy(scene)
    voices = tts_cfg["voices"]
    genders = scene.get("meta", {}).get("genders", {})
    if missing := sorted({genders[s] for s in {l["speaker"] for l in scene["script"]}
                          if s in genders and not voices.get(s) and not voices.get(genders[s])}):
        log.warning("Нет референс-голоса для пола %s (tts.voices в config/base.toml) — "
                    "эти роли озвучит встроенный голос", ", ".join(missing))
    # два героя одного пола — разные голоса: второй набор записей или сдвиг высоты
    role_voices, shifts = second_voices(scene["script"], voices, genders, tts_cfg["second_voice"])
    for speaker in sorted(set(shifts) | {s for s, v in role_voices.items() if v != voices.get(s)}):
        log.info("Роль %s — голос тот же, что у другой роли: %s", speaker,
                 f"записи со сдвигом {shifts[speaker]:+g} полутона" if speaker in shifts
                 else f"второй набор записей {role_voices[speaker]}")
    shifted_cache = output_dir(engine.cfg, "cache", "voices")

    parts, cursor, all_words = [], 0, []
    no_record: dict[Path, set[str]] = {}      # папка голоса → эмоции без своей записи
    for i, line in enumerate(scene["script"]):
        emotion = line.get("emotion") or DEFAULT_EMOTION
        gender = genders.get(line["speaker"])
        voice = resolve_voice(line["speaker"], role_voices, gender, emotion)
        if (source := voice_source(line["speaker"], role_voices, gender)) and not source.is_file():
            if voice is None or voice.stem != emotion:
                no_record.setdefault(source, set()).add(emotion)
        if voice is not None and line["speaker"] in shifts:
            voice = shifted_reference(voice, shifts[line["speaker"]], shifted_cache)
        line_params = emotion_params(params, tts_cfg["emotions"], emotion)
        if _SPEAKABLE.search(normalize_for_tts(line["text"], tts_cfg["language_id"])):
            audio, stressed, words = engine.synthesize_with_words(line["text"], line_params, voice,
                                                                  seed=seed + 1000 * i)
        else:   # сообщение из одних эмодзи: пузырь есть, голоса нет — короткая пауза, чтобы его заметили
            audio = np.zeros(int(tts_cfg["silent_line_ms"] / 1000 * engine.sr), np.float32)
            stressed, words = line["text"], []
        sr = engine.sr
        if i:
            gap = np.zeros(int(tts_cfg["pause_between_lines_ms"] / 1000 * sr), np.float32)
            parts.append(gap)
            cursor += len(gap)
        line["stressed_text"] = stressed
        line["timestamp_ms"] = round(cursor / sr * 1000)
        line["duration_ms"] = round(len(audio) / sr * 1000)
        offset = cursor / sr
        all_words += [Word(w.text, w.start + offset, w.end + offset, i) for w in words]
        parts.append(audio)
        cursor += len(audio)

    for folder, emotions in sorted(no_record.items()):
        log.warning("В %s нет записей %s — эти реплики озвучены %s", rel_path(folder),
                    ", ".join(f"{e}.wav" for e in sorted(emotions)),
                    "записью neutral.wav или другой эмоцией" if find_reference(folder, DEFAULT_EMOTION)
                    else "встроенным голосом (записей в папке нет)")

    full = np.concatenate(parts)
    peak = float(np.abs(full).max(initial=0.0))
    if peak > 0.99:   # без клиппинга при записи в 16 бит
        full = full * (0.99 / peak)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(out_path, full, engine.sr, subtype="PCM_16")

    scene["audio"] = {
        "path": rel_path(out_path),
        "duration_ms": round(len(full) / engine.sr * 1000),
        "tts_params": params,
    }
    if all_words:
        # Слова из проверки кусков whisper'ом — готовые таймкоды для субтитров
        words_path = out_path.with_suffix(".words.json")
        save_words(all_words, words_path)
        scene["audio"]["words_path"] = rel_path(words_path)
    return validate_scene(scene)


def stretch_scene_audio(scene: dict, target_ms: int, max_change: float = 0.07) -> dict | None:
    """Тонкая докрутка темпа всего аудио до target_ms (tts-stack-spec, раздел 9: не больше
    ±5–10%). Таймкоды реплик масштабируются. None — если нужно изменение больше max_change."""
    import librosa
    from core.scene import abs_path

    rate = scene["audio"]["duration_ms"] / target_ms          # > 1 — ускорить
    if abs(rate - 1) > max_change:
        return None
    path = abs_path(scene["audio"]["path"])
    audio, sr = sf.read(path, dtype="float32")
    stretched = librosa.effects.time_stretch(audio, rate=rate)
    sf.write(path, stretched, sr, subtype="PCM_16")

    scene = copy.deepcopy(scene)
    for line in scene["script"]:
        line["timestamp_ms"] = round(line["timestamp_ms"] / rate)
        line["duration_ms"] = round(line["duration_ms"] / rate)
    scene["audio"]["duration_ms"] = round(len(stretched) / sr * 1000)
    if words_path := scene["audio"].get("words_path"):
        words = load_words(abs_path(words_path))
        save_words([Word(w.text, w.start / rate, w.end / rate, w.line) for w in words], abs_path(words_path))
    return validate_scene(scene)
