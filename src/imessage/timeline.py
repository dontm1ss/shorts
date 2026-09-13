"""Таймлайн переписки: когда появляется каждый пузырь и когда начинается «набор».

Пузырь появляется в момент, когда начинает звучать его реплика (timestamp_ms из TTS) —
зритель читает синхронно с озвучкой. Перед сообщением собеседника показывается «печатает…»,
перед сообщением владельца — текст набирается в поле ввода.
"""
from __future__ import annotations

import hashlib


def _seeded(scene_id: str, salt: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha1(f"{scene_id}:{salt}".encode()).hexdigest(), 16)
    return lo + h % (hi - lo + 1)


def build_timeline(scene: dict, cfg: dict) -> dict:
    ic = cfg["imessage"]
    if not scene.get("script"):
        raise ValueError("В сцене нет script")
    if any("timestamp_ms" not in line for line in scene["script"]):
        raise ValueError("Сцена не озвучена — нет timestamp_ms у реплик")

    messages, prev_appear = [], None
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

    contacts = scene.get("meta", {}).get("contacts", {})
    other = next((s for s in ("A", "B") if s != ic["me_speaker"]), "A")
    # Время и счётчик непрочитанных — детерминированно от scene_id: ролики не выглядят одинаково
    sid = scene["scene_id"]
    hour, minute = _seeded(sid, "h", 18, 23), _seeded(sid, "m", 0, 59)
    total_ms = scene["audio"]["duration_ms"] + cfg["compositor"]["tail_ms"]
    return {
        "theme": ic["theme"],
        "contact": contacts.get(other) or "Контакт",
        "stamp": f"Сегодня {hour:02d}:{minute:02d}",
        "clock": f"{hour:02d}:{min(59, minute + 2 + total_ms // 60000):02d}",
        "badge": str(_seeded(sid, "b", 1, 99)),
        "receipt": "Доставлено",
        "messages": messages,
        "total_ms": total_ms,
    }
