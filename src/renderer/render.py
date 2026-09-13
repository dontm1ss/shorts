"""Финальный рендер: план композитора → ffmpeg с энкодером профиля (запасной — libx264)."""
from __future__ import annotations

import logging
from pathlib import Path

from compositor.layouts import build_plan
from core.media import MediaError, run_ffmpeg
from core.scene import rel_path, work_dir

log = logging.getLogger(__name__)


def render_scene(scene: dict, cfg: dict, ui_video: Path | None = None) -> dict:
    """Рендерит сцену в scene.output.path. Возвращает статистику {output, duration_s, elapsed_s, encoder}."""
    r = cfg["render"]
    attempts = [(r["encoder"], r["encoder_args"])]
    if r["encoder"] != r["fallback_encoder"]:
        attempts.append((r["fallback_encoder"], r["fallback_encoder_args"]))

    log_path = work_dir(cfg, scene) / "render.log"
    last_error = None
    for encoder, encoder_args in attempts:
        plan = build_plan(scene, cfg, encoder, encoder_args, ui_video)
        plan.output.parent.mkdir(parents=True, exist_ok=True)
        try:
            elapsed = run_ffmpeg(plan.args, cwd=plan.cwd, log_path=log_path)
        except MediaError as e:
            last_error = e
            log.warning("Рендер с %s не удался, пробую следующий энкодер:\n%s", encoder, e)
            continue
        log.info("Готово: %s — %.1f с видео за %.1f с (x%.2f реального времени, %s)",
                 rel_path(plan.output), plan.duration_s, elapsed, plan.duration_s / elapsed, encoder)
        return {"output": rel_path(plan.output), "duration_s": plan.duration_s,
                "elapsed_s": round(elapsed, 2), "encoder": encoder}
    raise last_error
