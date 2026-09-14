"""Оркестратор: ставит конвейеры в очередь и выполняет их.

    venv\\Scripts\\python -m pipeline story --count 3 [--topic "..."]   # DeepSeek придумывает истории сам
    venv\\Scripts\\python -m pipeline story --post data/forum-posts/reddit_xxx.json   # пересказ поста (старый путь)
    venv\\Scripts\\python -m pipeline drama [--topic "..."] [--count 2]
    venv\\Scripts\\python -m pipeline clips data/source-videos/film.mp4 [--limit 5] [--no-files]
    venv\\Scripts\\python -m pipeline worker [--watch]              # выполнить очередь
    venv\\Scripts\\python -m pipeline status | retry <id>

По умолчанию после постановки сразу запускается воркер; --no-run — только поставить в очередь.
"""
from __future__ import annotations

import argparse
import logging
import sys
import warnings
from pathlib import Path

from core.config import load_config
from core.scene import rel_path
from pipeline.worker import open_queue, run_jobs, run_worker


def _enqueue(cfg, kind: str, payloads: list[dict]) -> None:
    queue = open_queue(cfg)
    ids = [queue.enqueue(kind, p) for p in payloads]
    queue.close()
    logging.info("В очереди: %s", ", ".join(f"#{i} {kind}" for i in ids))


def cmd_story(args, cfg) -> list[dict]:
    """По умолчанию DeepSeek придумывает истории сам; --post / --scrape — пересказ постов с форумов."""
    posts: list[Path] = list(args.post or [])
    if args.scrape:
        from scraper import pending_posts, scrape
        scrape(cfg)
        posts += pending_posts(cfg)[:args.scrape]
    if posts:
        return [{"post": rel_path(p)} for p in posts]
    return [{"topic": args.topic} for _ in range(args.count)]


def cmd_status(args, cfg) -> int:
    queue = open_queue(cfg)
    for j in queue.jobs(limit=1000 if args.all else 30):
        extra = (j.result or {}).get("output") or (j.error or "").splitlines()[0:1]
        print(f"#{j.id:<5} {j.status:<9} {j.kind:<22} попыток {j.attempts}/{j.max_attempts}  "
              f"{extra if isinstance(extra, str) else ' '.join(extra)}")
    print("Итого:", queue.counts())
    queue.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    warnings.filterwarnings("ignore")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m pipeline")
    parser.add_argument("--profile")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("story", help="истории: DeepSeek придумывает сам")
    p.add_argument("--count", type=int, default=1, help="сколько историй придумать")
    p.add_argument("--topic", help="тема (необязательно)")
    p.add_argument("--post", type=Path, action="append", help="пересказать пост с форума (JSON, старый путь)")
    p.add_argument("--scrape", type=int, metavar="N", help="спарсить форумы (Reddit + Pikabu) и взять N лучших постов")
    p.add_argument("--no-run", action="store_true")

    p = sub.add_parser("drama", help="iMessage-драмы")
    p.add_argument("--topic")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--no-run", action="store_true")

    p = sub.add_parser("clips", help="нарезка собственных видео")
    p.add_argument("source", type=Path, help="видео или папка")
    p.add_argument("--limit", type=int, help="рендерить только первые N сегментов каждого видео")
    p.add_argument("--no-files", action="store_true", help="не сохранять сегменты отдельными mp4")
    p.add_argument("--no-run", action="store_true")

    p = sub.add_parser("worker", help="выполнить задачи очереди")
    p.add_argument("--watch", action="store_true", help="не завершаться, ждать новые задачи")

    p = sub.add_parser("status", help="состояние очереди")
    p.add_argument("--all", action="store_true")

    p = sub.add_parser("retry", help="повторить упавшую задачу")
    p.add_argument("job_id", type=int)

    p = sub.add_parser("job", help=argparse.SUPPRESS)
    p.add_argument("job_ids", type=int, nargs="+")

    args = parser.parse_args(argv)
    cfg = load_config(args.profile)

    if args.cmd == "job":
        return run_jobs(args.job_ids, cfg)
    if args.cmd == "status":
        return cmd_status(args, cfg)
    if args.cmd == "retry":
        queue = open_queue(cfg)
        queue.retry(args.job_id)
        queue.close()
        logging.info("#%d и зависящие задачи возвращены в очередь", args.job_id)
        return 0

    if args.cmd == "story":
        _enqueue(cfg, "script_story", cmd_story(args, cfg))
    elif args.cmd == "drama":
        _enqueue(cfg, "script_drama", [{"topic": args.topic}] * args.count)
    elif args.cmd == "clips":
        from video_clipper.clipper import find_source_videos
        videos = find_source_videos(cfg, args.source) if args.source.is_dir() else [args.source]
        _enqueue(cfg, "video_clip_extraction", [
            {"video": rel_path(v.resolve()), "write_files": False if args.no_files else None,
             "render_limit": args.limit} for v in videos])

    if getattr(args, "no_run", False):
        return 0
    logging.info("Профиль %s: запускаю воркер", cfg["profile"]["name"])
    counts = run_worker(cfg, stop_when_empty=not getattr(args, "watch", False))
    logging.info("Очередь: %s", counts)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
