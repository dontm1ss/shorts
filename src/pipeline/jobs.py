"""Обработчики задач очереди. Выполняются в отдельном процессе (python -m pipeline job <id> [<id>…]):
обычно одна задача на процесс, озвучка — пачкой (JobContext держит модель TTS между задачами).
Обработчик возвращает result; ключ "next" — цепочки задач, которые нужно поставить после этой:
    {"next": [[{kind, payload}, ...], ...]}   — каждая цепочка последовательна, цепочки независимы.

Состояние сцены между этапами — файл output/scenes/<scene_id>.json.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from core.scene import abs_path, load_scene, output_dir, rel_path, save_scene, scene_path, work_dir

log = logging.getLogger(__name__)


def _scene_file(payload: dict) -> Path:
    return abs_path(payload["scene"])


class JobContext:
    """Общие ресурсы задач одного процесса. Модели TTS и whisper-проверки грузятся один раз
    на пачку задач озвучки, а не на каждый ролик."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._tts = None

    def tts_engine(self):
        if self._tts is None:
            from tts import TTSEngine
            self._tts = TTSEngine(self.cfg)
        return self._tts

    def close(self) -> None:
        if self._tts is not None:
            self._tts.unload()
            self._tts = None


# --- сценарии -------------------------------------------------------------------

def _after_script(scene: dict, path: Path, cfg: dict) -> dict:
    """Сценарий готов: на отбор в веб-панель (review.enabled) или сразу в работу
    (отбор выключен или включено автоодобрение)."""
    from pipeline.review import auto_approve, mark_auto_approved, mark_pending, processing_chain
    if cfg["review"]["enabled"]:
        if not auto_approve(cfg):
            save_scene(mark_pending(scene), path)
            log.info("Ждёт решения в панели отбора (python -m panel)")
            return {"scene": rel_path(path), "review": "pending"}
        mark_auto_approved(scene)
        log.info("Автоодобрение — сразу в озвучку")
    save_scene(scene, path)
    return {"scene": rel_path(path), "next": [processing_chain(scene, cfg, path)]}


def script_batch(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    """N историй: берёт лучшие неиспользованные посты, при нехватке — сначала парсит форумы."""
    from scraper import pending_posts, scrape
    count = int(payload.get("count", 1))
    posts = pending_posts(cfg)
    if len(posts) < count:
        scrape(cfg)
        posts = pending_posts(cfg)
    posts = posts[:count]
    if not posts:
        raise RuntimeError("Нет новых постов — источники ничего не вернули (лимиты Reddit?)")
    log.info("Постов для историй: %d из %d запрошенных", len(posts), count)
    return {"posts": len(posts),
            "next": [[{"kind": "script_story", "payload": {"post": rel_path(p)}}] for p in posts]}

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


def script_story(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    """История. Основной путь — DeepSeek придумывает её сам (payload: topic — необязательно).
    С payload["post"] — пересказ поста с форума (старый путь, --post / --scrape)."""
    from scripting import DeepSeekClient, adapt_story, invent_story, story_scene
    client = DeepSeekClient(cfg)
    if "post" in payload:
        from scraper.store import mark_processed
        post = json.loads(abs_path(payload["post"]).read_text(encoding="utf-8"))
        scene = story_scene(adapt_story(post, client, cfg), cfg,
                            {k: post.get(k) for k in ("id", "source", "url", "title")})
        mark_processed(cfg, post["id"])
    else:
        topic = (payload.get("topic") or "").strip() or None
        scene = story_scene(invent_story(client, cfg, topic), cfg, topic=topic)
    scene = _prefit(scene, cfg, client)
    path = scene_path(cfg, scene["scene_id"])
    log.info("История «%s» → %s", scene["meta"]["title"], rel_path(path))
    return _after_script(scene, path, cfg)


def script_drama(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    from scripting import DeepSeekClient, drama_scene, generate_drama
    client = DeepSeekClient(cfg)
    topic = payload.get("topic")
    scene = _prefit(drama_scene(generate_drama(client, cfg, topic), cfg, topic), cfg, client)
    path = scene_path(cfg, scene["scene_id"])
    log.info("Переписка «%s» (%d сообщений) → %s", scene["meta"]["title"], len(scene["script"]), rel_path(path))
    return _after_script(scene, path, cfg)


# --- тяжёлые этапы --------------------------------------------------------------

def tts_synthesis(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    """Озвучка. По умолчанию — один проход, без подгонки под 50–60 с: рендер сам обрезает
    видео по концу озвучки (scene_duration_s), так что точный тайминг не нужен, а докрутка
    темпа и повторные вызовы DeepSeek только тратят время на медленном TTS. С fit=True в
    payload — старое поведение: небольшой промах (≤7%) докручивается темпом, большой —
    DeepSeek переписывает текст и озвучка повторяется (до story.max_fit_rounds раз)."""
    from tts import synthesize_scene
    from tts.scene import stretch_scene_audio
    path = _scene_file(payload)
    scene = load_scene(path)
    s = cfg["story"]
    lo_ms, hi_ms = s["min_duration_s"] * 1000, s["max_duration_s"] * 1000
    own_ctx = ctx is None                 # одиночный вызов — модель выгружается сразу
    ctx = ctx or JobContext(cfg)
    engine = ctx.tts_engine()
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
        if own_ctx:
            ctx.close()
    save_scene(voiced, path)
    return {"duration_s": voiced["audio"]["duration_ms"] / 1000, "audio": voiced["audio"]["path"]}


def subtitle_generation(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    from subtitles import subtitle_scene
    path = _scene_file(payload)
    scene = subtitle_scene(load_scene(path), cfg, use_whisper=payload.get("whisper", True))
    save_scene(scene, path)
    return {"subtitles": scene["subtitles"]["path"]}


def ui_render(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    from imessage import record_scene
    path = _scene_file(payload)
    scene = load_scene(path)
    out = record_scene(scene, cfg, work_dir(cfg, scene) / "ui.mov")
    return {"ui_video": rel_path(out)}


def video_render(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    from compositor import pick_background, scene_duration_s
    from pipeline.manifest import record as record_manifest
    from renderer import render_scene
    path = _scene_file(payload)
    scene = load_scene(path)
    if "background" not in scene:
        scene["background"] = pick_background(cfg, round(scene_duration_s(scene, cfg) * 1000))
        save_scene(scene, path)
    ui = work_dir(cfg, scene) / "ui.mov" if scene["type"] == "imessage_drama" else None
    if ui is not None and not ui.exists():   # удалена автоочисткой после прошлого рендера
        from imessage import record_scene
        record_scene(scene, cfg, ui)
    result = render_scene(scene, cfg, ui_video=ui)
    record_manifest(cfg, scene, result)
    _cleanup(scene, cfg)
    return result


def _cleanup(scene: dict, cfg: dict) -> None:
    """Удалить тяжёлые промежуточные файлы сцены (cleanup.after_render)."""
    freed = 0
    for name in cfg["cleanup"]["after_render"]:
        path = work_dir(cfg, scene) / name
        if path.exists():
            freed += path.stat().st_size
            path.unlink()
    if freed:
        log.info("Очистка: освобождено %.0f МБ", freed / 2**20)


def video_clip_extraction(payload: dict, cfg: dict, ctx: JobContext | None = None) -> dict:
    from subtitles import transcribe_source
    from video_clipper import cut_video
    source = abs_path(payload["video"])
    scenes = cut_video(source, cfg, write_files=payload.get("write_files"))
    limit = payload.get("render_limit")
    if "video_clip" in cfg["compositor"]["subtitles_for"] and limit != 0:
        # Один проход whisper на весь исходник вместо отдельного на каждый отрезок: окно 30 с
        # с шагом 2 с — иначе каждая секунда распознавалась бы 15 раз
        words = rel_path(transcribe_source(source, cfg, output_dir(cfg, "clips", source.stem) / "words.json"))
        for scene in scenes:
            scene["source_video"]["words_path"] = words
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
    "script_batch": script_batch,
    "script_story": script_story,
    "script_drama": script_drama,
    "tts_synthesis": tts_synthesis,
    "subtitle_generation": subtitle_generation,
    "ui_render": ui_render,
    "video_render": video_render,
    "video_clip_extraction": video_clip_extraction,
}
