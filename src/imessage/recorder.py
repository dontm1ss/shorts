"""Запись интерфейса iMessage в видео: Playwright (системный Edge/Chrome) → PNG-кадры → ffmpeg.

Кадры снимаются детерминированно (renderAt(t)), а не записью экрана в реальном времени:
видео точно совпадает с озвучкой на любом железе, даже если скриншоты медленные (i3).
Кадры без изменений не снимаются повторно — большая часть ролика статична.

Фон страницы прозрачный (style.css) — Playwright снимает PNG с настоящей альфой
(omit_background), а промежуточное видео пишется кодеком с альфа-каналом (qtrle/.mov,
без потерь, лёгкий по CPU — быстрее любого альтернативного alpha-кодека, а такой
промежуточный файл не идёт в финальный рендер и не должен быть маленьким). h264/yuv420p
здесь не подходит — там альфы нет вообще, и фон под сообщениями стал бы снова непрозрачным.
"""
from __future__ import annotations

import logging
import math
import subprocess
import time
from pathlib import Path

from imessage.timeline import build_timeline

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent / "web"


def _launch(playwright, channel: str):
    """Системный браузер по каналу; если его нет — скачанный Playwright Chromium."""
    try:
        if channel and channel != "chromium":
            return playwright.chromium.launch(channel=channel)
    except Exception as e:
        log.warning("Браузер %r не запустился (%s) — пробую встроенный Chromium", channel, str(e).splitlines()[0])
    try:
        return playwright.chromium.launch()
    except Exception as e:
        raise RuntimeError("Нет браузера для записи iMessage: установите Edge/Chrome или выполните "
                           "`venv\\Scripts\\python -m playwright install chromium`") from e


def record_timeline(timeline: dict, cfg: dict, out_path: Path) -> Path:
    from playwright.sync_api import sync_playwright

    ic, v = cfg["imessage"], cfg["video"]
    fps = v["fps"]
    scale = v["width"] / ic["css_width"]
    css_h = round(cfg["compositor"]["ui_height"] / scale)
    frames = math.ceil(timeline["total_ms"] / 1000 * fps)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    ffmpeg = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "image2pipe", "-framerate", str(fps), "-c:v", "png", "-i", "-",
         "-c:v", "qtrle", "-pix_fmt", "argb", str(out_path)],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)

    t0 = time.perf_counter()
    shots = 0
    try:
        with sync_playwright() as p:
            browser = _launch(p, ic["browser_channel"])
            page = browser.new_page(viewport={"width": ic["css_width"], "height": css_h},
                                    device_scale_factor=scale)
            page.goto((WEB_DIR / "index.html").as_uri())
            page.evaluate("data => window.init(data)", timeline)
            last_key, png = None, None
            for f in range(frames):
                key = page.evaluate("t => window.renderAt(t)", f * 1000 / fps)
                if key != last_key:
                    png = page.screenshot(type="png", omit_background=True)
                    last_key = key
                    shots += 1
                ffmpeg.stdin.write(png)
            browser.close()
    finally:
        ffmpeg.stdin.close()
        code = ffmpeg.wait()
    if code:
        raise RuntimeError(f"ffmpeg (запись iMessage) завершился с кодом {code}: "
                           f"{ffmpeg.stderr.read().decode(errors='replace')[-800:]}")
    log.info("iMessage: %d кадров (%d уникальных) за %.1f с → %s",
             frames, shots, time.perf_counter() - t0, out_path.name)
    return out_path


def record_scene(scene: dict, cfg: dict, out_path: Path) -> Path:
    return record_timeline(build_timeline(scene, cfg), cfg, out_path)
