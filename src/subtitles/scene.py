"""Субтитры для сцены: озвученная сцена → words.json + subs.ass + subs.srt + блок subtitles."""
from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

from core.media import extract_audio
from core.scene import abs_path, rel_path, validate_scene, work_dir
from subtitles.align import Word, script_words, transcribe
from subtitles.formats import group_words, write_ass, write_srt

log = logging.getLogger(__name__)


def scene_words(scene: dict, cfg: dict, use_whisper: bool = True, tmp_dir: Path | None = None) -> list[Word]:
    s = cfg["subtitles"]
    if scene.get("script"):
        if "audio" not in scene:
            raise ValueError("Сцена не озвучена — нет блока audio")
        asr = None
        if use_whisper:
            prompt = " ".join(line["text"] for line in scene["script"])[-400:]
            asr = transcribe(abs_path(scene["audio"]["path"]), cfg, language=s["language"], prompt=prompt)
        return script_words(scene, asr)

    if scene["type"] == "video_clip":
        # Нет сценария — субтитры по оригинальному звуку отрезка
        sv = scene["source_video"]
        wav = extract_audio(abs_path(sv["path"]), (tmp_dir or work_dir(cfg, scene)) / "clip_audio.wav",
                            sv["clip_start_ms"], sv["clip_end_ms"] - sv["clip_start_ms"])
        language = None if s["clip_language"] == "auto" else s["clip_language"]
        return transcribe(wav, cfg, language=language)
    raise ValueError("Нечего субтитровать: в сцене нет script")


def subtitle_scene(scene: dict, cfg: dict, use_whisper: bool = True, out_dir: Path | None = None) -> dict:
    s = cfg["subtitles"]
    out_dir = out_dir or work_dir(cfg, scene)
    out_dir.mkdir(parents=True, exist_ok=True)
    words = scene_words(scene, cfg, use_whisper, out_dir)
    groups = group_words(words, s["max_words"], s["max_chars"], s["max_gap_s"])
    layout = cfg["compositor"]["layout"][scene["type"]]
    ass = write_ass(groups, out_dir / "subs.ass", s, layout, cfg["video"]["width"], cfg["video"]["height"])
    write_srt(groups, out_dir / "subs.srt", s["uppercase"])
    (out_dir / "words.json").write_text(
        json.dumps([w.to_dict() for w in words], ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("Субтитры: %d слов, %d титров → %s", len(words), len(groups), rel_path(ass))

    scene = copy.deepcopy(scene)
    scene["subtitles"] = {"path": rel_path(ass), "format": "ass"}
    return validate_scene(scene)
