"""Фоновые видео нижнего слоя (ТЗ 1, раздел 2.7): пул роликов в config/base.toml
([[backgrounds]]) — на каждую сцену случайно выбирается один ролик и случайный отрезок
нужной длины через seek."""
from __future__ import annotations

import random

from core.media import media_info
from core.scene import abs_path, rel_path


def pick_background(cfg: dict, duration_ms: int, rng: random.Random | None = None, lead_ms: int = 0) -> dict:
    """Блок background для сцены: случайный ролик из пула + случайный start_ms, чтобы отрезок
    целиком влез в файл. Если фон короче ролика — старт с нуля (рендерер зациклит фон).
    lead_ms — запас фона перед стартом (петля: в конце фон переходит в кадры перед своим началом)."""
    r = rng or random
    bg = r.choice(cfg["backgrounds"])
    path = abs_path(bg["path"])
    total = media_info(path)["duration_ms"]
    latest_start = total - duration_ms
    start = r.randint(min(lead_ms, latest_start), latest_start) if latest_start > 0 else 0
    return {"type": bg["name"], "source_path": rel_path(path), "start_ms": start}
