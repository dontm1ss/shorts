"""CLI записи интерфейса iMessage для озвученной сцены.

    venv\\Scripts\\python -m imessage output/scenes/<scene_id>.json [--out ui.mov]
    venv\\Scripts\\python -m imessage --preview output/scenes/<scene_id>.json   # один кадр PNG
"""
import argparse
import logging
import sys
from pathlib import Path

from core.config import load_config
from core.scene import load_scene, work_dir
from imessage.recorder import WEB_DIR, _launch
from imessage.timeline import build_timeline
from imessage import record_scene


def preview(scene: dict, cfg: dict, out: Path, t_ms: float | None) -> Path:
    from playwright.sync_api import sync_playwright
    timeline = build_timeline(scene, cfg)
    ic, v = cfg["imessage"], cfg["video"]
    scale = v["width"] / ic["css_width"]
    with sync_playwright() as p:
        browser = _launch(p, ic["browser_channel"])
        page = browser.new_page(viewport={"width": ic["css_width"],
                                          "height": round(cfg["compositor"]["ui_height"] / scale)},
                                device_scale_factor=scale)
        page.goto((WEB_DIR / "index.html").as_uri())
        page.evaluate("data => window.init(data)", timeline)
        page.evaluate("t => window.renderAt(t)", timeline["total_ms"] if t_ms is None else t_ms)
        page.screenshot(path=str(out))
        browser.close()
    return out


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m imessage")
    parser.add_argument("scene", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--preview", action="store_true", help="один кадр PNG вместо видео")
    parser.add_argument("--at", type=float, help="момент кадра для --preview, мс (по умолчанию — конец)")
    parser.add_argument("--profile")
    args = parser.parse_args(argv)

    cfg = load_config(args.profile)
    scene = load_scene(args.scene)
    if args.preview:
        out = preview(scene, cfg, args.out or work_dir(cfg, scene) / "ui_preview.png", args.at)
    else:
        out = record_scene(scene, cfg, args.out or work_dir(cfg, scene) / "ui.mov")
    logging.info("→ %s", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
