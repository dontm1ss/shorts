"""Сводки обработчика очереди для Telegram. Упавшие задачи — одним сообщением через
notify.failures_batch_s после первой (обрыв VPN валит десятки сценариев разом). Готовые ролики
и сценарии на проверке — когда очередь опустела, а пока работа идёт — не чаще раза
в notify.ready_digest_min минут. Ошибка внутри сводки только пишется в лог: обработчик
из-за уведомлений не останавливается.
"""
from __future__ import annotations

import contextlib
import json
import logging
import time

from core.scene import abs_path
from notify.telegram import notify

log = logging.getLogger(__name__)

KIND_LABEL = {"script_story": "сценарий истории", "script_drama": "сценарий переписки",
              "script_batch": "сбор постов", "tts_synthesis": "озвучка", "subtitle_generation": "субтитры",
              "ui_render": "запись переписки", "video_render": "рендер", "video_clip_extraction": "нарезка"}
TYPE_LABEL = {"reddit_story": "история", "imessage_drama": "переписка", "video_clip": "клип"}


def _scene(job) -> dict:
    try:
        return json.loads(abs_path(job.payload["scene"]).read_text(encoding="utf-8"))
    except (KeyError, OSError, ValueError):
        return {}


def _job_label(job) -> str:
    title = _scene(job).get("meta", {}).get("title")
    return f"#{job.id} {KIND_LABEL.get(job.kind, job.kind)}" + (f" «{title}»" if title else "")


def _last_line(error: str) -> str:
    lines = [line.strip() for line in error.strip().splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


@contextlib.contextmanager
def _safe():
    try:
        yield
    except Exception:
        log.exception("Сводка для Telegram")


class Digest:
    def __init__(self, cfg: dict, send=None, clock=time.monotonic):
        self.cfg, self.send, self.clock = cfg, send or notify, clock
        self.failed: list[str] = []
        self.failed_since: float | None = None
        self.ready: list[str] = []
        self.review = 0
        self.last_ready = clock()

    def job_done(self, job, result: dict) -> None:
        with _safe():
            if job.kind == "video_render":
                scene = _scene(job)
                meta = scene.get("meta", {})
                self.ready.append(f"«{meta.get('title') or 'без названия'}» — "
                                  f"{TYPE_LABEL.get(scene.get('type'), 'ролик')}{' ↻' if meta.get('loop') else ''}, "
                                  f"{round(result.get('duration_s', 0))} с")
            elif result.get("review") == "pending":
                self.review += 1

    def job_failed(self, job, error: str) -> None:
        with _safe():
            self.failed.append(f"{_job_label(job)}: {_last_line(error)}")
            if self.failed_since is None:
                self.failed_since = self.clock()

    def tick(self, idle: bool) -> None:
        with _safe():
            n = self.cfg["notify"]
            if self.failed and self.clock() - self.failed_since >= n["failures_batch_s"]:
                self._send_failures()
            if (self.ready or self.review) and (idle or self.clock() - self.last_ready >= n["ready_digest_min"] * 60):
                self._send_ready()

    def flush(self) -> None:
        with _safe():
            if self.failed:
                self._send_failures()
            if self.ready or self.review:
                self._send_ready()

    def _send_failures(self) -> None:
        if len(self.failed) == 1:
            text = f"❌ Не удалась задача {self.failed[0]}"
        else:
            shown = "\n".join(f"• {line}" for line in self.failed[:10])
            more = f"\n…и ещё {len(self.failed) - 10}" if len(self.failed) > 10 else ""
            text = f"❌ Не удалось задач: {len(self.failed)}\n{shown}{more}"
        self.failed, self.failed_since = [], None
        self.send(self.cfg, text + self._panel_link())

    def _send_ready(self) -> None:
        parts = []
        if self.ready:
            shown = "\n".join(f"• {line}" for line in self.ready[:15])
            more = f"\n…и ещё {len(self.ready) - 15}" if len(self.ready) > 15 else ""
            parts.append(f"✅ Готово роликов: {len(self.ready)}\n{shown}{more}")
        if self.review:
            parts.append(f"📝 Новых сценариев на проверке: {self.review}")
        self.ready, self.review, self.last_ready = [], 0, self.clock()
        self.send(self.cfg, "\n\n".join(parts) + self._panel_link())

    def _panel_link(self) -> str:
        url = self.cfg["notify"].get("panel_url")
        return f"\n\nПанель: {url}" if url else ""
