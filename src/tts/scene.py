"""Озвучка сцены: реплики по ролям → один wav + таймкоды реплик + блок audio."""
from __future__ import annotations

import copy
from pathlib import Path

import numpy as np
import soundfile as sf

from core.config import ROOT
from core.scene import SceneError, rel_path, validate_scene, work_dir
from tts.engine import TTSEngine


def resolve_voice(speaker: str, voices: dict) -> Path | None:
    """Референс-голос роли из tts.voices; None — встроенный голос Chatterbox."""
    value = voices.get(speaker, "")
    if not value:
        return None
    path = Path(value) if Path(value).is_absolute() else ROOT / value
    if not path.exists():
        raise FileNotFoundError(f"Референс-голос для роли {speaker!r} не найден: {path}")
    return path


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

    parts, cursor = [], 0
    for i, line in enumerate(scene["script"]):
        voice = resolve_voice(line["speaker"], tts_cfg["voices"])
        audio, stressed = engine.synthesize(line["text"], params, voice, seed=seed + 1000 * i)
        sr = engine.sr
        if i:
            gap = np.zeros(int(tts_cfg["pause_between_lines_ms"] / 1000 * sr), np.float32)
            parts.append(gap)
            cursor += len(gap)
        line["stressed_text"] = stressed
        line["timestamp_ms"] = round(cursor / sr * 1000)
        line["duration_ms"] = round(len(audio) / sr * 1000)
        parts.append(audio)
        cursor += len(audio)

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
    return validate_scene(scene)
