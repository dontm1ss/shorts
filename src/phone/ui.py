"""Нажатия по UI-селекторам для приложений на телефоне (общее для post и warmup).

Селекторы — словари из phone.selectors: {"text": ...}, {"desc_contains": ...}, {"resource_id": ...},
{"xpath": ...} или {"coord": [доля_x, доля_y]}. Перебираем варианты по порядку, пока не сработает.
"""
from __future__ import annotations

import time


def locator(d, candidate: dict):
    if "text" in candidate:
        return d(text=candidate["text"])
    if "text_contains" in candidate:
        return d(textContains=candidate["text_contains"])
    if "desc" in candidate:
        return d(description=candidate["desc"])
    if "desc_contains" in candidate:
        return d(descriptionContains=candidate["desc_contains"])
    if "resource_id" in candidate:
        return d(resourceId=candidate["resource_id"])
    return None


def tap(d, candidates: list[dict], timeout: int = 15, settle: float = 2.0) -> bool:
    """Нажать первый сработавший вариант. False — ни один не нашёлся."""
    for candidate in candidates:
        if "coord" in candidate:
            x, y = candidate["coord"]
            w, h = d.window_size()
            d.click(int(w * x), int(h * y))
            time.sleep(settle)
            return True
        if "xpath" in candidate:
            node = d.xpath(candidate["xpath"]).get(timeout=timeout)
            if node is not None:
                node.click()
                time.sleep(settle)
                return True
            continue
        loc = locator(d, candidate)
        if loc is None:
            continue
        try:
            if loc.exists(timeout=timeout):
                loc.click()
                time.sleep(settle)
                return True
        except Exception:
            continue
    return False
