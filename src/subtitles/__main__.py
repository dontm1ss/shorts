"""CLI субтитров: сцена (озвученная или video_clip) → subs.ass / subs.srt, сцена обновляется на месте.

    venv\\Scripts\\python -m subtitles output/scenes/<scene_id>.json [--no-whisper]
"""
import argparse
import logging
import sys
import warnings
from pathlib import Path

from core.config import load_config
from core.scene import load_scene, save_scene
from subtitles.scene import subtitle_scene


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m subtitles")
    parser.add_argument("scene", type=Path)
    parser.add_argument("--no-whisper", action="store_true",
                        help="без распознавания: время реплики делится между словами пропорционально")
    parser.add_argument("--out", type=Path, help="куда сохранить сцену (по умолчанию — на место)")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    cfg = load_config(args.profile)
    scene = subtitle_scene(load_scene(args.scene), cfg, use_whisper=not args.no_whisper)
    save_scene(scene, args.out or args.scene)
    return 0


if __name__ == "__main__":
    sys.exit(main())
