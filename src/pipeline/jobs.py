"""Обработчики задач очереди. Каждый выполняется в отдельном процессе (python -m pipeline job <id>)
и возвращает result; ключ "next" — цепочки задач, которые нужно поставить после этой:
    {"next": [[{kind, payload}, ...], ...]}   — каждая цепочка последовательна, цепочки независимы.

Состояние сцены между этапами — файл output/scenes/<scene_id>.json.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from core.scene import abs_path, load_scene, rel_path, save_scene, scene_path, work_dir

log = logging.getLogger(__name__)


def _scene_file(payload: dict) -> Path:
    return abs_path(payload["scene"])


# --- сценарии -------------------------------------------------------------------

def _prefit(scene: dict, cfg: dict, client) -> dict:
    """Подгонка длины по оценке темпа — до дорогого синтеза."""
    from scripting import estimate_duration_s, rewrite_scene_length
    s = cfg["story"]
    for _ in range(2):
        est = estimate_duration_s(scene["script"], cfg)
        if s["min_duration_s"] <= est <= s["max_duration_s"]:
            break
        log.info("Оценка длительности %.1f с вне %d–%d — переписываю до TTS", est,
                 s["min_duration_s"], s["max_duration_s"])
        scene = rewrite_scene_length(scene, est, client, cfg)
    return scene


def script_story(payload: dict, cfg: dict) -> dict:
    from scraper.store import mark_processed
    from scripting import DeepSeekClient, adapt_story, story_scene
    post = json.loads(abs_path(payload["post"]).read_text(encoding="utf-8"))
    client = DeepSeekClient(cfg)
    story = adapt_story(post, client, cfg)
    scene = _prefit(story_scene(story, cfg, {k: post.get(k) for k in ("id", "source", "url", "title")}),
                    cfg, client)
    path = scene_path(cfg, scene["scene_id"])
    save_scene(scene, path)
    mark_processed(cfg, post["id"])
    log.info("История «%s» → %s", scene["meta"]["title"], rel_path(path))
    p = {"scene": rel_path(path)}
    return {"scene": rel_path(path), "next": [[
        {"kind": "tts_synthesis", "payload": {**p, "fit": True}},
        {"kind": "subtitle_generation", "payload": p},
        {"kind": "video_render", "payload": p},
    ]]}


def script_drama(payload: dict, cfg: dict) -> dict:
    from scripting import DeepSeekClient, drama_scene, generate_drama
    client = DeepSeekClient(cfg)
    topic = payload.get("topic")
    scene = _prefit(drama_scene(generate_drama(client, cfg, topic), cfg, topic), cfg, client)
    path = scene_path(cfg, scene["scene_id"])
    save_scene(scene, path)
    log.info("Переписка «%s» (%d сообщений) → %s", scene["meta"]["title"], len(scene["script"]), rel_path(path))
    p = {"scene": rel_path(path)}
    chain = [{"kind": "tts_synthesis", "payload": {**p, "fit": True}}]
    if "imessage_drama" in cfg["compositor"]["subtitles_for"]:
        chain.append({"kind": "subtitle_generation", "payload": p})
    chain += [{"kind": "ui_render", "payload": p}, {"kind": "video_render", "payload": p}]
    return {"scene": rel_path(path), "next": [chain]}


# --- тяжёлые этапы --------------------------------------------------------------

def tts_synthesis(payload: dict, cfg: dict) -> dict:
    """Озвучка; с fit=True — подгонка под 50–60 с: небольшой промах (≤7%) — докрутка темпа,
    большой — DeepSeek переписывает текст и озвучка повторяется."""
    from tts import TTSEngine, synthesize_scene
    from tts.scene import stretch_scene_audio
    path = _scene_file(payload)
    scene = load_scene(path)
    s = cfg["story"]
    lo_ms, hi_ms = s["min_duration_s"] * 1000, s["max_duration_s"] * 1000
    engine = TTSEngine(cfg)
    client = None
    try:
        for round_ in range(s["max_fit_rounds"] + 1):
            voiced = synthesize_scene(scene, engine)
            duration_ms = voiced["audio"]["duration_ms"]
            log.info("Озвучка: %.1f с (раунд %d)", duration_ms / 1000, round_ + 1)
            if not payload.get("fit") or lo_ms <= duration_ms <= hi_ms:
                break
            # цель — граница диапазона с запасом 0.5 с
            target = hi_ms - 500 if duration_ms > hi_ms else lo_ms + 500
            if stretched := stretch_scene_audio(voiced, target):
                log.info("Докрутка темпа: %.1f → %.1f с", duration_ms / 1000, stretched["audio"]["duration_ms"] / 1000)
                voiced = stretched
                break
            if round_ == s["max_fit_rounds"]:
                log.warning("Не удалось уложиться в %d–%d с за %d раундов — оставляю %.1f с",
                            s["min_duration_s"], s["max_duration_s"], s["max_fit_rounds"], duration_ms / 1000)
                break
            from scripting import DeepSeekClient, rewrite_scene_length
            client = client or DeepSeekClient(cfg)
            scene = rewrite_scene_length(scene, duration_ms / 1000, client, cfg)
    finally:
        engine.unload()
    save_scene(voiced, path)
    return {"duration_s": voiced["audio"]["duration_ms"] / 1000, "audio": voiced["audio"]["path"]}


def subtitle_generation(payload: dict, cfg: dict) -> dict:
    from subtitles import subtitle_scene
    path = _scene_file(payload)
    scene = subtitle_scene(load_scene(path), cfg, use_whisper=payload.get("whisper", True))
    save_scene(scene, path)
    return {"subtitles": scene["subtitles"]["path"]}


def ui_render(payload: dict, cfg: dict) -> dict:
    from imessage import record_scene
    path = _scene_file(payload)
    scene = load_scene(path)
    out = record_scene(scene, cfg, work_dir(cfg, scene) / "ui.mp4")
    return {"ui_video": rel_path(out)}


def video_render(payload: dict, cfg: dict) -> dict:
    from compositor import pick_background, scene_duration_s
    from renderer import render_scene
    path = _scene_file(payload)
    scene = load_scene(path)
    if "background" not in scene:
        scene["background"] = pick_background(cfg, round(scene_duration_s(scene, cfg) * 1000))
        save_scene(scene, path)
    ui = work_dir(cfg, scene) / "ui.mp4" if scene["type"] == "imessage_drama" else None
    return render_scene(scene, cfg, ui_video=ui)


def video_clip_extraction(payload: dict, cfg: dict) -> dict:
    from video_clipper import cut_video
    scenes = cut_video(abs_path(payload["video"]), cfg, write_files=payload.get("write_files"))
    limit = payload.get("render_limit")
    chains = []
    for scene in scenes:
        path = scene_path(cfg, scene["scene_id"])
        save_scene(scene, path)
        if limit is None or len(chains) < limit:
            p = {"scene": rel_path(path)}
            chains.append([{"kind": "subtitle_generation", "payload": p},
                           {"kind": "video_render", "payload": p}])
    return {"segments": len(scenes), "next": chains}


HANDLERS = {
    "script_story": script_story,
    "script_drama": script_drama,
    "tts_synthesis": tts_synthesis,
    "subtitle_generation": subtitle_generation,
    "ui_render": ui_render,
    "video_render": video_render,
    "video_clip_extraction": video_clip_extraction,
}
