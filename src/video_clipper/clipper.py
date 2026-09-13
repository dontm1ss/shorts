"""Нарезка исходных видео скользящим окном (ТЗ 1.2): окно 30 с, шаг 2 с между стартами.

Хвостовые отрезки, у которых окно выходит за конец видео, короче окна — без паддинга.
Для каждого отрезка создаётся сцена video_clip (метаданные) и, если включено, файл mp4.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from core.media import media_info, run_ffmpeg
from core.scene import output_dir, rel_path, validate_scene, video_path

log = logging.getLogger(__name__)


def plan_segments(duration_ms: int, window_ms: int, step_ms: int) -> list[tuple[int, int]]:
    """[(start_ms, end_ms)]: старты 0, step, 2·step… пока старт внутри видео."""
    if window_ms <= 0 or step_ms <= 0:
        raise ValueError("window_ms и step_ms должны быть > 0")
    return [(start, min(start + window_ms, duration_ms)) for start in range(0, duration_ms, step_ms)]


def clip_scene(source: Path, start_ms: int, end_ms: int, cfg: dict) -> dict:
    scene_id = str(uuid.uuid4())
    v = cfg["video"]
    return validate_scene({
        "scene_id": scene_id,
        "type": "video_clip",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_video": {"path": rel_path(source), "clip_start_ms": start_ms, "clip_end_ms": end_ms},
        "output": {"resolution": f"{v['width']}x{v['height']}",
                   "path": rel_path(video_path(cfg, "video_clip", scene_id))},
    })


def segment_filename(source: Path, index: int, start_ms: int, end_ms: int) -> str:
    return f"{source.stem}_{index:03d}_{start_ms // 1000:05d}-{end_ms // 1000:05d}.mp4"


def extract_segment(source: Path, start_ms: int, end_ms: int, dst: Path, cfg: dict,
                    encoder: str | None = None, encoder_args: list[str] | None = None) -> None:
    """Точный рез с перекодированием (копирование потоков режет только по ключевым кадрам)."""
    r = cfg["render"]
    encoder = encoder or r["encoder"]
    encoder_args = r["encoder_args"] if encoder_args is None else encoder_args
    run_ffmpeg(["-ss", f"{start_ms / 1000:.3f}", "-i", str(source), "-t", f"{(end_ms - start_ms) / 1000:.3f}",
                "-map", "0:v:0", "-map", "0:a?", "-c:v", encoder, *encoder_args, "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(dst)])


def cut_video(source: Path, cfg: dict, write_files: bool | None = None,
              out_dir: Path | None = None) -> list[dict]:
    """Нарезает одно видео. Возвращает сцены video_clip; пишет segments.json с метаданными."""
    c = cfg["clipper"]
    write_files = c["write_files"] if write_files is None else write_files
    info = media_info(source)
    segments = plan_segments(info["duration_ms"], c["window_s"] * 1000, c["step_s"] * 1000)
    out_dir = out_dir or output_dir(cfg, "clips", source.stem)
    out_dir.mkdir(parents=True, exist_ok=True)
    log.info("%s: %.1f с → %d сегментов", source.name, info["duration_ms"] / 1000, len(segments))

    encoder, encoder_args = cfg["render"]["encoder"], cfg["render"]["encoder_args"]
    scenes, meta = [], []
    for i, (start, end) in enumerate(segments):
        scene = clip_scene(source, start, end, cfg)
        entry = {"index": i, "scene_id": scene["scene_id"], "source": rel_path(source),
                 "clip_start_ms": start, "clip_end_ms": end, "file": None}
        if write_files:
            dst = out_dir / segment_filename(source, i, start, end)
            try:
                extract_segment(source, start, end, dst, cfg, encoder, encoder_args)
            except Exception:
                if encoder == cfg["render"]["fallback_encoder"]:
                    raise
                log.warning("Энкодер %s не сработал — дальше %s", encoder, cfg["render"]["fallback_encoder"])
                encoder, encoder_args = cfg["render"]["fallback_encoder"], cfg["render"]["fallback_encoder_args"]
                extract_segment(source, start, end, dst, cfg, encoder, encoder_args)
            entry["file"] = rel_path(dst)
        scenes.append(scene)
        meta.append(entry)

    (out_dir / "segments.json").write_text(
        json.dumps({"source": rel_path(source), "duration_ms": info["duration_ms"],
                    "window_ms": c["window_s"] * 1000, "step_ms": c["step_s"] * 1000,
                    "segments": meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    return scenes


def find_source_videos(cfg: dict, folder: Path) -> list[Path]:
    exts = {e.lower() for e in cfg["clipper"]["video_extensions"]}
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in exts)
