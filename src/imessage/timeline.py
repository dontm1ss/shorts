"""Таймлайн переписки: когда появляется каждый пузырь и когда начинается «набор».

Пузырь появляется в момент, когда начинает звучать его реплика (timestamp_ms из TTS) —
зритель читает синхронно с озвучкой. Перед сообщением собеседника показывается «печатает…».

Петля (meta.loop): ролик крутится по кругу, и перед первым сообщением стоит последнее — сообщения
прошлого круга рисует web/app.js, а «печатает…» перед первым сообщением следующего круга
начинается ещё в конце ролика (wrap). Стык выглядит как переход к очередному сообщению.
"""
from __future__ import annotations

import hashlib

from compositor.layouts import scene_duration_s
from core.scene import is_loop


def _seeded(scene_id: str, salt: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha1(f"{scene_id}:{salt}".encode()).hexdigest(), 16)
    return lo + h % (hi - lo + 1)


def build_timeline(scene: dict, cfg: dict) -> dict:
    ic = cfg["imessage"]
    if not scene.get("script"):
        raise ValueError("В сцене нет script")
    if any("timestamp_ms" not in line for line in scene["script"]):
        raise ValueError("Сцена не озвучена — нет timestamp_ms у реплик")

    loop = is_loop(scene)
    total_ms = round(scene_duration_s(scene, cfg) * 1000, 3)
    # у петли первому сообщению предшествует последнее — из прошлого круга, на период раньше
    prev_appear = scene["script"][-1]["timestamp_ms"] - total_ms if loop else None
    messages = []
    for line in scene["script"]:
        appear = line["timestamp_ms"]
        earliest = 0 if prev_appear is None else prev_appear + ic["min_gap_ms"]
        messages.append({
            "side": "right" if line["speaker"] == ic["me_speaker"] else "left",
            "text": line["text"],
            "appear_ms": appear,
            "typing_start_ms": min(appear, max(earliest, appear - ic["typing_ms"])),
        })
        prev_appear = appear

    # Время — детерминированно от scene_id, чтобы ролики не выглядели одинаково
    sid = scene["scene_id"]
    start = _seeded(sid, "h", 18, 23) * 60 + _seeded(sid, "m", 0, 55)
    read = start + _seeded(sid, "r", 1, 4)
    timeline = {
        "theme": ic["theme"],
        "stamp_day": "Сегодня",
        "stamp_time": f"{start // 60:02d}:{start % 60:02d}",
        "receipt_label": "Прочитано",
        "receipt_time": f"{read // 60 % 24:02d}:{read % 60:02d}",
        "messages": messages,
        "total_ms": total_ms,
    }
    if loop:
        first = messages[0]
        timeline["loop"] = True
        # первое сообщение следующего круга: «печатает…» перед ним — в конце этого
        timeline["wrap"] = {"side": first["side"], "typing_start_ms": first["typing_start_ms"] + total_ms}
    return timeline
