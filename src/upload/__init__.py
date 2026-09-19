"""Выгрузка готовых роликов на площадки: YouTube (Data API v3) и TikTok (Content Posting API).

Тексты берутся из `output/publish/<scene_id>.json` (их пишет задача publish_meta), файл ролика —
из манифеста. Кто отправляет — учётка панели: у каждой свои привязанные канал и аккаунт
(`upload.links`), отправленное запоминается в `upload.history`.
"""
from __future__ import annotations

import logging
from pathlib import Path

from core.scene import output_dir
from pipeline import manifest
from pipeline.publish import load_publish
from upload import history, links

log = logging.getLogger(__name__)

PLATFORMS = ("youtube", "tiktok")


class UploadError(RuntimeError):
    pass


def platform_module(platform: str):
    if platform == "youtube":
        from upload import youtube
        return youtube
    if platform == "tiktok":
        from upload import tiktok
        return tiktok
    raise UploadError(f"Неизвестная площадка: {platform}")


def texts_for(cfg: dict, scene_id: str, entry: dict) -> dict:
    """Заголовок, описание и теги ролика. Тексты пишет DeepSeek при одобрении; если их ещё нет
    (старый ролик, задача упала) — берём хотя бы название из манифеста, чтобы отправка не встала."""
    texts = load_publish(cfg, scene_id) or {}
    title = texts.get("shorts_title") or entry.get("title") or "Shorts"
    return {"title": title,
            "description": texts.get("shorts_description") or "",
            "tiktok": texts.get("tiktok") or title,
            "hashtags": texts.get("hashtags") or []}


def upload_scene(cfg: dict, scene_id: str, platform: str, user: str, progress=None) -> dict:
    """Отправить готовый ролик на площадку от имени учётки панели."""
    if platform not in PLATFORMS:
        raise UploadError(f"Неизвестная площадка: {platform}")
    if not cfg[platform]["enabled"]:
        raise UploadError(f"Выгрузка на {platform} выключена в конфиге")
    entry = manifest.find(cfg, scene_id)
    if entry is None:
        raise UploadError("Нет такого ролика")
    if not links.get(cfg, user, platform):
        raise UploadError(f"У учётки {user} не привязан {platform} — личный кабинет")
    # путь берём через paths.output, а не как записано в манифесте: папку вывода можно переносить
    video = output_dir(cfg, "videos", Path(entry.get("output", "")).name)
    if not video.is_file():
        raise UploadError(f"Файл ролика не найден: {entry.get('output', '')}")
    module = platform_module(platform)
    texts = texts_for(cfg, scene_id, entry)
    if platform == "youtube":
        body = module.video_body(cfg, texts["title"], texts["description"], texts["hashtags"])
        result = module.upload(cfg, user, video, body, progress)
    else:
        result = module.upload(cfg, user, video, texts["tiktok"], progress)
    history.record(cfg, scene_id, platform, result)
    log.info("%s: ролик «%s» отправлен учёткой %s", platform, texts["title"], user)
    return result
