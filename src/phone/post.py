"""Публикация готового ролика через приложение на телефоне (TikTok / YouTube Shorts / Reels).

Официальный API TikTok без аудита умеет только слать ролик в черновики (upload.tiktok),
а прямая публикация — только «видно мне». Поэтому публиковать публично удобнее с телефона:
ролик кладётся на устройство (adb push), дальше uiautomator2 жмёт кнопки приложения.

Селекторы интерфейса — в phone.selectors: их чинят там при обновлении приложений.
Каждый шаг, который не удалось найти, снимается в output/phone/ и возвращается с понятной
ошибкой — смотреть, что реально на экране, и править селекторы.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

from core.scene import output_dir
from phone import PhoneError, accounts, device, selectors, ui
from pipeline import manifest
from upload import history as upload_history
from upload import texts_for

log = logging.getLogger(__name__)

STEP_WAIT_S = 15      # сколько ждать появления элемента на шаге


def find_video(cfg: dict, scene_id: str) -> Path:
    """Путь к готовому ролику по scene_id (из манифеста)."""
    entry = manifest.find(cfg, scene_id)
    if entry is None:
        raise PhoneError("Нет такого ролика")
    video = output_dir(cfg, "videos", Path(entry.get("output", "")).name)
    if not video.is_file():
        raise PhoneError(f"Файл ролика не найден: {entry.get('output', '')}")
    return video


def remote_path(cfg: dict, local: Path) -> str:
    """Куда на телефоне положить файл (phone.remote_dir + имя файла)."""
    base = str(cfg["phone"]["remote_dir"]).rstrip("/")
    return f"{base}/{local.name}"


def push_video(cfg: dict, video: Path, serial: str | None = None) -> str:
    remote = remote_path(cfg, video)
    log.info("Телефон: %s → %s", video, remote)
    device.push(cfg, video, remote, serial=serial)
    device.media_scan(cfg, remote, serial=serial)
    return remote


def run_flow(d, platform: str, caption: str, cfg: dict) -> None:
    """Пройти поток публикации по шагам phone.selectors, вписав подпись на нужном шаге."""
    steps = selectors.flow(platform)
    table = selectors.SELECTORS[platform]
    for step in steps:
        if not ui.tap(d, table[step], timeout=STEP_WAIT_S):
            shot = device.screenshot(cfg, output_dir(cfg, "phone", f"{platform}_{step}.png"))
            raise PhoneError(
                f"Шаг «{step}» ({platform}) не найден на экране. Снимок: {shot}. "
                f"Поправьте селекторы в src/phone/selectors.py (weditor)."
            )
        if step == "caption" and caption:
            d.send_keys(caption, clear=True)
        log.info("телефон %s: шаг %s готов", platform, step)


def post(cfg: dict, scene_id: str, platform: str, caption: str | None = None,
         serial: str | None = None) -> dict:
    """Опубликовать готовый ролик с телефона. platform: tiktok | youtube | instagram."""
    if platform not in selectors.SELECTORS:
        raise PhoneError(f"Неизвестная площадка: {platform}")
    entry = manifest.find(cfg, scene_id)
    if entry is None:
        raise PhoneError("Нет такого ролика")
    video = find_video(cfg, scene_id)
    if caption is None:
        texts = texts_for(cfg, scene_id, entry)
        caption = texts["tiktok"] if platform in ("tiktok", "instagram") else texts["title"]
        tags = " ".join(texts["hashtags"]).strip()
        if tags and platform != "youtube":
            caption = f"{caption} {tags}".strip()
    remote = push_video(cfg, video, serial=serial)
    d = device.u2(cfg, serial)
    package = cfg["phone"]["packages"][platform]
    device.app_start(cfg, package, serial=serial)
    time.sleep(6)  # приложение открывается
    run_flow(d, platform, caption, cfg)
    result = {"scene_id": scene_id, "platform": platform, "remote": remote, "caption": caption,
              "posted_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "mode": "phone"}
    # публикация с телефона видна панели как обычная выгрузка, а аккаунт становится «активным»
    upload_history.record(cfg, scene_id, platform, result)
    accounts.record_post(cfg, platform, scene_id)
    return result
