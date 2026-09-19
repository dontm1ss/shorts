"""Прогрев аккаунтов TikTok / YouTube Shorts / Instagram Reels: «человеческие» сессии просмотра.

Прогрев нужен, чтобы новый аккаунт не выглядел ботом: площадка видит обычный просмотр ленты
со случайными паузами. Сессия собирается в план (build_plan) и исполняется на устройстве.
Контент — по тематике проекта (истории, переписки, нарезки): в части сессий открываем поиск
по ключевику из `phone.warmup.keywords`, чтобы алгоритм подбирал похожую аудиторию.

План строится детерминированно от seed — его можно протестировать без телефона.
Исполнение требует подключённого телефона (uiautomator2). После сессии счётчики и статус
аккаунта обновляются в phone.accounts.
"""
from __future__ import annotations

import logging
import random
import time

from phone import PhoneError, accounts, device, selectors, ui

log = logging.getLogger(__name__)

PLATFORMS = ("tiktok", "youtube", "instagram")

# действия плана: ("watch", секунды) / ("idle", секунды без действия) / ("swipe_up", ~сек) /
# ("swipe_down", ~сек) / ("like", 0) / ("follow", 0)
WATCH = "watch"
IDLE = "idle"
SWIPE_UP = "swipe_up"
SWIPE_DOWN = "swipe_down"
LIKE = "like"
FOLLOW = "follow"


def build_plan(cfg: dict, seed: int | None = None) -> list[tuple[str, float]]:
    """Собрать план одной сессии прогрева. Случайность — от seed (None — непредсказуемо)."""
    w = cfg["phone"]["warmup"]
    rng = random.Random(seed)
    session = rng.uniform(w["session_min_s"], w["session_max_s"])
    plan: list[tuple[str, float]] = []
    elapsed = 0.0
    while elapsed < session:
        watch = rng.uniform(w["watch_min_s"], w["watch_max_s"])
        plan.append((WATCH, watch))
        elapsed += watch
        roll = rng.random()
        if roll < w["like_probability"]:
            plan.append((LIKE, 0.0))
        elif roll < w["like_probability"] + w["follow_probability"]:
            plan.append((FOLLOW, 0.0))
        plan.append((SWIPE_UP, 0.0))
        elapsed += 1.0
        if rng.random() < w["swipe_back_probability"]:
            plan.append((SWIPE_DOWN, 0.0))
            elapsed += 1.0
        if rng.random() < 0.2:
            pause = rng.uniform(3, 15)
            plan.append((IDLE, pause))
            elapsed += pause
    return plan


def _coords(d, x: float, y: float) -> tuple[int, int]:
    w, h = d.window_size()
    return int(w * x), int(h * y)


def _search(d, platform: str, keyword: str) -> bool:
    """Открыть поиск, ввести ключевик, открыть первый результат. False — не вышло (тогда лента)."""
    table = selectors.WARMUP[platform]
    if not ui.tap(d, table["search_open"], timeout=6):
        return False
    time.sleep(1.5)
    if not ui.tap(d, table["search_field"], timeout=6):
        return False
    d.send_keys(keyword, clear=True)
    time.sleep(1.5)
    d.press("enter")
    time.sleep(4)
    ui.tap(d, table["first_result"], timeout=6)
    time.sleep(3)
    return True


def _open_feed(d, platform: str) -> None:
    """Без поиска: открыть ленту Shorts/Reels (у TikTok лента и так открыта)."""
    key = {"youtube": "shorts", "instagram": "reels"}.get(platform)
    if key:
        ui.tap(d, selectors.WARMUP[platform][key], timeout=6)
        time.sleep(2)


def _execute(d, plan: list[tuple[str, float]]) -> None:
    for step, (action, seconds) in enumerate(plan, 1):
        if action in (WATCH, IDLE):
            time.sleep(seconds)
        elif action == SWIPE_UP:
            d.swipe(*_coords(d, 0.5, 0.8), *_coords(d, 0.5, 0.2), duration=0.4)
        elif action == SWIPE_DOWN:
            d.swipe(*_coords(d, 0.5, 0.2), *_coords(d, 0.5, 0.8), duration=0.4)
        elif action == LIKE:
            d.double_click(*_coords(d, 0.85, 0.5))
        elif action == FOLLOW:
            d.click(*_coords(d, 0.1, 0.6))
        log.info("прогрев %d/%d: %s %.0fс", step, len(plan), action, seconds)


def run(cfg: dict, platform: str = "tiktok", serial: str | None = None,
        seed: int | None = None) -> dict:
    """Сессия прогрева аккаунта площадки на подключённом телефоне."""
    if platform not in PLATFORMS:
        raise PhoneError(f"Неизвестная площадка прогрева: {platform}")
    w = cfg["phone"]["warmup"]
    rng = random.Random(seed)
    plan = build_plan(cfg, seed)
    d = device.u2(cfg, serial)
    package = cfg["phone"]["packages"][platform]
    log.info("Прогрев %s: запускаю %s, шагов %d", platform, package, len(plan))
    device.app_start(cfg, package, serial)
    time.sleep(8)                                   # приложение грузится
    keyword = None
    if rng.random() < float(w.get("search_probability", 0.6)):
        keywords = w.get("keywords") or []
        keyword = rng.choice(keywords) if keywords else None
        if keyword and _search(d, platform, keyword):
            log.info("Прогрев %s: контент по ключевику «%s»", platform, keyword)
        else:
            keyword = None
            _open_feed(d, platform)
    else:
        _open_feed(d, platform)
    started = time.monotonic()
    _execute(d, plan)
    planned_s = sum(seconds for _, seconds in plan)
    rec = accounts.record_warmup(cfg, platform, seconds=planned_s, actions=len(plan))
    result = {"platform": platform, "actions": len(plan), "planned_s": int(planned_s),
              "wall_s": int(time.monotonic() - started), "keyword": keyword,
              "status": rec["status"], "warmup_sessions": rec["warmup_sessions"]}
    log.info("Прогрев %s завершён: %s, статус %s", platform, result, rec["status"])
    return result
