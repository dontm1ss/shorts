"""CLI нарезчика: видео (или папка) → сегменты + сцены video_clip в output/scenes/.

    venv\\Scripts\\python -m video_clipper data/source-videos/film.mp4 [--no-files]
    venv\\Scripts\\python -m video_clipper data/source-videos            # все видео папки
"""
import argparse
import logging
import sys
from pathlib import Path

from core.config import load_config
from core.scene import save_scene, scene_path
from video_clipper.clipper import cut_video, find_source_videos


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m video_clipper")
    parser.add_argument("source", type=Path, help="видеофайл или папка")
    parser.add_argument("--no-files", action="store_true", help="только сцены/метаданные, без mp4")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    cfg = load_config(args.profile)
    sources = find_source_videos(cfg, args.source) if args.source.is_dir() else [args.source]
    total = 0
    for source in sources:
        scenes = cut_video(source, cfg, write_files=False if args.no_files else None)
        for scene in scenes:
            save_scene(scene, scene_path(cfg, scene["scene_id"]))
        total += len(scenes)
    logging.info("Готово: %d видео, %d сцен video_clip", len(sources), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
