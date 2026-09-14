"""Субтитры для сцены: озвученная сцена → words.json + subs.ass + subs.srt + блок subtitles.

Откуда берутся таймкоды слов (whisper запускается только в крайнем случае):
- озвученная сцена — из проверки кусков whisper'ом при озвучке (audio.words_path);
- отрезок клипа — из одного распознавания всего исходника (source_video.words_path):
  отрезки идут окном 30 с с шагом 2 с, каждую секунду исходника иначе распознавали бы 15 раз;
- иначе — whisper по аудио сцены / отрезка.
"""
from __future__ import annotations

import copy
import hashlib
import json
import logging
from pathlib import Path

from core.media import extract_audio, media_info
from core.scene import abs_path, rel_path, validate_scene, work_dir
from subtitles.align import Word, clip_words, load_words, save_words, script_words, transcribe
from subtitles.formats import group_words, write_ass, write_srt

log = logging.getLogger(__name__)


def _clip_language(cfg: dict) -> str | None:
    lang = cfg["subtitles"]["clip_language"]
    return None if lang == "auto" else lang


def transcribe_source(video: Path, cfg: dict, out_json: Path) -> Path:
    """Один проход whisper по всему исходнику → слова со временем от начала исходника."""
    words: list[Word] = []
    if media_info(video)["has_audio"]:
        wav = extract_audio(video, out_json.with_suffix(".wav"))
        try:
            words = transcribe(wav, cfg, language=_clip_language(cfg))
        finally:
            wav.unlink(missing_ok=True)
    save_words(words, out_json)
    log.info("Распознан исходник %s: %d слов → %s", video.name, len(words), rel_path(out_json))
    return out_json


def scene_words(scene: dict, cfg: dict, use_whisper: bool = True, tmp_dir: Path | None = None) -> list[Word]:
    s = cfg["subtitles"]
    if scene.get("script"):
        if "audio" not in scene:
            raise ValueError("Сцена не озвучена — нет блока audio")
        asr = None
        if use_whisper:
            words_path = scene["audio"].get("words_path")
            if words_path and abs_path(words_path).exists():
                asr = load_words(abs_path(words_path))
                log.info("Таймкоды слов — из проверки озвучки, без повторного распознавания")
            else:
                prompt = " ".join(line["text"] for line in scene["script"])[-400:]
                asr = transcribe(abs_path(scene["audio"]["path"]), cfg, language=s["language"], prompt=prompt)
        return script_words(scene, asr)

    if scene["type"] == "video_clip":
        # Нет сценария — субтитры по оригинальному звуку отрезка
        sv = scene["source_video"]
        start_s, end_s = sv["clip_start_ms"] / 1000, sv["clip_end_ms"] / 1000
        words_path = sv.get("words_path")
        if words_path and abs_path(words_path).exists():
            log.info("Таймкоды слов — из общего распознавания исходника")
            return clip_words(load_words(abs_path(words_path)), start_s, end_s)
        wav = extract_audio(abs_path(sv["path"]), (tmp_dir or work_dir(cfg, scene)) / "clip_audio.wav",
                            sv["clip_start_ms"], sv["clip_end_ms"] - sv["clip_start_ms"])
        return transcribe(wav, cfg, language=_clip_language(cfg))
    raise ValueError("Нечего субтитровать: в сцене нет script")


def pick_style(scene: dict, cfg: dict) -> dict:
    """Шрифт и цвет подсветки сцены (subtitles.variety): сохранённый в meta.style, иначе выбор
    по scene_id — детерминированно, чтобы перерендер давал тот же вид. {} — общий стиль."""
    if saved := scene.get("meta", {}).get("style"):
        return saved
    variety = cfg["subtitles"].get("variety", {})
    if not variety.get("enabled"):
        return {}
    fonts, colors = variety["fonts"], variety["highlight_colors"]
    h = int(hashlib.sha1(scene["scene_id"].encode()).hexdigest(), 16)
    font = fonts[h % len(fonts)]
    return {"font": font["name"], "size": font["size"],
            "highlight_color": colors[(h // len(fonts)) % len(colors)]}


def subtitle_scene(scene: dict, cfg: dict, use_whisper: bool = True, out_dir: Path | None = None) -> dict:
    s = cfg["subtitles"]
    out_dir = out_dir or work_dir(cfg, scene)
    out_dir.mkdir(parents=True, exist_ok=True)
    words = scene_words(scene, cfg, use_whisper, out_dir)
    groups = group_words(words, s["max_words"], s["max_chars"], s["max_gap_s"])
    layout = cfg["compositor"]["layout"][scene["type"]]
    style = pick_style(scene, cfg)
    sub_cfg = {**s, "style": {**s["style"], **style}}
    ass = write_ass(groups, out_dir / "subs.ass", sub_cfg, layout, cfg["video"]["width"], cfg["video"]["height"])
    write_srt(groups, out_dir / "subs.srt", s["uppercase"])
    (out_dir / "words.json").write_text(
        json.dumps([w.to_dict() for w in words], ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Субтитры: %d слов, %d титров, %s → %s", len(words), len(groups),
             f"{style['font']} / {style['highlight_color']}" if style else "общий стиль", rel_path(ass))

    scene = copy.deepcopy(scene)
    scene["subtitles"] = {"path": rel_path(ass), "format": "ass"}
    if style:
        scene.setdefault("meta", {})["style"] = style
    return validate_scene(scene)
