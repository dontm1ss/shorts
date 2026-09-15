"""Бот фермы в Telegram — работает в потоке панели.

Присылает сценарии на проверку с кнопками «Одобрить / Отклонить / Переписать» (правка — ответом на сообщение
бота), готовые ролики — видео с текстами для публикации (подпись TikTok, заголовок и описание Shorts) и кнопкой
«Часть N»; отвечает на /status. Сбои шлёт обработчик очереди (notify.Digest).

Подключение чата — по ссылке из панели t.me/<бот>?start=<код>: код виден только в панели под паролем,
поэтому посторонний, нашедший бота, подключиться не может. Сообщения из чужих чатов бот не слушает.

Состояние — output/telegram-state.json: сдвиг обновлений, что уже отправлено, чего ждёт правка. При первом
запуске готовые ролики считаются отправленными (без потока старых роликов в чат), а из ждущих проверки
присылаются только 10 последних.
"""
from __future__ import annotations

import html
import json
import logging
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path

from core.scene import output_dir, scene_path
from notify.telegram import TelegramError, call, credentials, save_env
from pipeline import manifest
from pipeline.publish import load_publish
from pipeline.review import ReviewError, list_scenes, reject, revising, start_revision
from scripting import estimate_duration_s

log = logging.getLogger(__name__)

TYPE_LABEL = {"reddit_story": "история", "imessage_drama": "переписка", "video_clip": "клип"}
VIDEO_LIMIT = 49 * 2 ** 20          # бот отправляет файлы до 50 МБ
TEXTS_WAIT_S = 600                  # тексты для публикации ещё пишутся — ролик ждёт их не дольше
FIRST_SCRIPTS = 10                  # при первом запуске — сколько ждущих проверки прислать


def esc(text) -> str:
    return html.escape(str(text or ""), quote=False)


def _version(scene: dict) -> str:
    """Версия сценария на проверке: переписанный — новая, его присылаем заново."""
    review = scene.get("meta", {}).get("review", {})
    return review.get("revised_at") or review.get("created_at") or scene.get("created_at", "")


def script_message(scene: dict, cfg: dict) -> str:
    meta = scene.get("meta", {})
    script = scene.get("script", [])
    head = [TYPE_LABEL.get(scene["type"], scene["type"]) + (" ↻ петля" if meta.get("loop") else "")]
    if score := meta.get("score"):
        head.append(f"★ {score['total']}/10")
    head.append(f"≈ {round(estimate_duration_s(script, cfg))} с")
    lines = [f"📝 <b>{esc(meta.get('title') or 'Без названия')}</b>", " · ".join(head)]
    if score and score.get("verdict"):
        lines.append(f"<i>{esc(score['verdict'])}</i>")
    if scene["type"] == "imessage_drama":
        names = meta.get("contacts", {})
        body = "\n".join(f"<b>{esc(names.get(l['speaker'], l['speaker']))}:</b> {esc(l['text'])}" for l in script)
        if meta.get("loop") and script:
            body += f"\n<i>↻ дальше снова: {esc(script[0]['text'])}</i>"
    else:
        body = "\n\n".join(esc(l["text"]) for l in script)
    text = "\n".join(lines) + "\n\n" + body
    return text if len(text) <= 4000 else text[:3990] + "…"


def publish_message(texts: dict) -> str:
    parts = [("TikTok", texts.get("tiktok")), ("Shorts — заголовок", texts.get("shorts_title")),
             ("Shorts — описание", texts.get("shorts_description")), ("Хэштеги", " ".join(texts.get("hashtags", [])))]
    return "\n\n".join(f"<b>{name}</b> (нажмите, чтобы скопировать)\n<code>{esc(value)}</code>"
                       for name, value in parts if value)


def _age_s(iso: str | None) -> float:
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except (TypeError, ValueError):
        return float("inf")


class FarmBot:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.pair_code = f"{secrets.randbelow(10 ** 8):08d}"
        self.username: str | None = None
        self.state: dict | None = self._load_state()
        self._lock = threading.Lock()

    # --- для панели ----------------------------------------------------------------------

    def info(self) -> dict:
        token, chat = credentials()
        link = f"https://t.me/{self.username}?start={self.pair_code}" if token and self.username and not chat else None
        return {"configured": bool(token), "connected": bool(token and chat), "bot": self.username, "pair_link": link}

    # --- состояние -----------------------------------------------------------------------

    def _state_path(self) -> Path:
        return output_dir(self.cfg, "telegram-state.json")

    def _load_state(self) -> dict | None:
        try:
            return json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save_state(self) -> None:
        path = self._state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    def _initial_state(self) -> dict:
        pending = list_scenes(self.cfg, "pending")                     # новые первыми
        return {"offset": 0, "videos": [e.get("scene_id") for e in manifest.entries(self.cfg)],
                "scripts": {s["scene_id"]: _version(s) for s in pending[FIRST_SCRIPTS:]}, "notes": {}}

    # --- цикл ----------------------------------------------------------------------------

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                if not self.tick(stop):
                    stop.wait(30)                                       # токена нет — ждать, пока впишут
            except TelegramError as e:
                log.warning("Бот Telegram: %s", e)
                stop.wait(15)
            except Exception:
                log.exception("Бот Telegram")
                stop.wait(15)

    def tick(self, stop: threading.Event | None = None, poll_s: int = 25) -> bool:
        """Одно ожидание обновлений (long polling) + рассылка нового. False — бот не настроен."""
        token, chat = credentials()
        if not token:
            return False
        if self.username is None:
            self.username = call(token, "getMe")["username"]
        with self._lock:
            if self.state is None:
                self.state = self._initial_state()
            updates = call(token, "getUpdates", http_timeout=poll_s + 15, offset=self.state["offset"], timeout=poll_s,
                           allowed_updates=["message", "callback_query"])
            for update in updates:
                self.state["offset"] = update["update_id"] + 1
                self._handle(token, chat, update)
            self._save_state()
            token, chat = credentials()                                 # чат мог только что подключиться
            if chat:
                self._announce(token, chat, stop)
        return True

    # --- входящие ------------------------------------------------------------------------

    def _send(self, token: str, chat: str, text: str, **extra) -> dict:
        return call(token, "sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                    disable_web_page_preview=True, **extra)

    def _handle(self, token: str, chat: str | None, update: dict) -> None:
        if "callback_query" in update:
            if chat:
                self._on_button(token, chat, update["callback_query"])
            return
        msg = update.get("message") or {}
        sender = str((msg.get("chat") or {}).get("id", ""))
        text = (msg.get("text") or "").strip()
        if not chat:                                                    # ещё не подключён — только по коду
            if (msg.get("chat") or {}).get("type") == "private" and text.startswith("/start"):
                if text.split()[1:2] == [self.pair_code]:
                    save_env("TELEGRAM_CHAT_ID", sender)
                    log.info("Telegram: чат владельца подключён")
                    self._send(token, sender, "✅ Бот фермы подключён. Сюда будут приходить сценарии на проверку, "
                                              "готовые ролики с текстами для публикации и сбои. /status — очередь.")
                else:
                    self._send(token, sender, "Это бот фермы shorts. Подключается кнопкой «Подключить Telegram» в панели.")
            return
        if sender != chat:                                              # чужие чаты не слушаем
            return
        reply_to = str((msg.get("reply_to_message") or {}).get("message_id", ""))
        if reply_to in self.state["notes"] and text:
            self._revise(token, chat, self.state["notes"].pop(reply_to), text)
        elif text.startswith("/status"):
            self._send(token, chat, self._status())
        elif text.startswith(("/start", "/help")):
            self._send(token, chat, "Бот фермы shorts: сценарии на проверку — с кнопками, готовые ролики — с текстами "
                                    "для публикации (нажмите на текст, чтобы скопировать). /status — очередь.")

    def _on_button(self, token: str, chat: str, query: dict) -> None:
        from panel import app as panel                                  # панель запускает бота, бот — её действия
        action, _, scene_id = (query.get("data") or "").partition(":")
        message = query.get("message") or {}
        answer, done = "", None
        if str((query.get("from") or {}).get("id")) != chat:
            answer = "Это не ваш бот"
        else:
            try:
                if action == "ap":
                    panel.approve_and_start(self.cfg, scene_id)
                    answer, done = "Одобрен — в озвучку и рендер", "✅ Одобрен"
                elif action == "rj":
                    reject(self.cfg, scene_id)
                    answer, done = "Отклонён", "❌ Отклонён"
                elif action == "rw":
                    asked = self._send(token, chat, "Что изменить? Ответьте на это сообщение — DeepSeek перепишет "
                                                    "сценарий и пришлёт новую версию.",
                                       reply_markup={"force_reply": True, "input_field_placeholder": "концовку жёстче"})
                    self.state["notes"][str(asked["message_id"])] = scene_id
                    answer = "Напишите, что изменить"
                elif action == "sq":
                    _, new = panel.enqueue_once(self.cfg, "script_sequel", scene_id)
                    panel.start_worker(self.cfg)
                    answer = "Пишу продолжение — пришлю на проверку" if new else "Продолжение уже пишется"
            except ReviewError as e:
                answer = str(e)
        call(token, "answerCallbackQuery", callback_query_id=query["id"], text=answer[:190], show_alert=False)
        if done and message:
            call(token, "editMessageReplyMarkup", chat_id=chat, message_id=message["message_id"],
                 reply_markup={"inline_keyboard": [[{"text": done, "callback_data": "noop"}]]})

    def _revise(self, token: str, chat: str, scene_id: str, note: str) -> None:
        from panel import app as panel
        try:
            start_revision(self.cfg, scene_id, note)
        except ReviewError as e:
            self._send(token, chat, f"Не получилось: {esc(e)}")
            return
        threading.Thread(target=panel.revise_in_background, args=(self.cfg, scene_id, note),
                         name=f"revise-{scene_id[:8]}", daemon=True).start()
        self._send(token, chat, "✎ DeepSeek переписывает — пришлю новую версию")

    def _status(self) -> str:
        from panel import app as panel
        queue = panel.open_queue(self.cfg)
        try:
            counts = queue.counts()
        finally:
            queue.close()
        return (f"📊 Очередь: ждут {counts.get('pending', 0)}, идут {counts.get('running', 0)}, "
                f"упало {counts.get('failed', 0)}\n"
                f"Обработчик: {'работает' if panel.worker_running(self.cfg) else 'остановлен'}\n"
                f"На проверке: {len(list_scenes(self.cfg, 'pending'))}\n"
                f"Готовых роликов: {len(manifest.entries(self.cfg))}")

    # --- рассылка ------------------------------------------------------------------------

    def _announce(self, token: str, chat: str, stop: threading.Event | None) -> None:
        pending = list_scenes(self.cfg, "pending")
        for scene in reversed(pending):                                 # старые первыми
            version = _version(scene)
            if revising(scene) or self.state["scripts"].get(scene["scene_id"]) == version:
                continue
            markup = {"inline_keyboard": [
                [{"text": "✅ Одобрить", "callback_data": f"ap:{scene['scene_id']}"},
                 {"text": "❌ Отклонить", "callback_data": f"rj:{scene['scene_id']}"}],
                [{"text": "✎ Переписать", "callback_data": f"rw:{scene['scene_id']}"}]]}
            self._send(token, chat, script_message(scene, self.cfg), reply_markup=markup)
            self.state["scripts"][scene["scene_id"]] = version
            self._save_state()
            if stop is not None and stop.wait(1):
                return
        waiting = {s["scene_id"] for s in pending}
        self.state["scripts"] = {k: v for k, v in self.state["scripts"].items() if k in waiting}

        sent = set(self.state["videos"])
        for entry in manifest.entries(self.cfg):
            scene_id = entry.get("scene_id")
            if not scene_id or scene_id in sent:
                continue
            writable = entry.get("type") in ("reddit_story", "imessage_drama")
            texts = load_publish(self.cfg, scene_id)
            if writable and texts is None and _age_s(entry.get("rendered_at")) < TEXTS_WAIT_S:
                continue                                                # тексты для публикации ещё пишутся
            self._send_video(token, chat, entry, texts, writable)
            self.state["videos"] = (self.state["videos"] + [scene_id])[-5000:]
            self._save_state()
            if stop is not None and stop.wait(1):
                return

    def _send_video(self, token: str, chat: str, entry: dict, texts: dict | None, writable: bool) -> None:
        meta = {}
        try:
            meta = json.loads(scene_path(self.cfg, entry["scene_id"]).read_text(encoding="utf-8")).get("meta", {})
        except (OSError, ValueError):
            pass
        kind = TYPE_LABEL.get(entry.get("type"), "ролик") + (" ↻" if entry.get("loop") else "")
        caption = f"🎬 <b>{esc(entry.get('title') or kind)}</b> — {kind}, {round(entry.get('duration_s') or 0)} с"
        markup = ({"inline_keyboard": [[{"text": f"📺 Часть {int(meta.get('part', 1)) + 1}",
                                         "callback_data": f"sq:{entry['scene_id']}"}]]} if writable else None)
        extra = {"reply_markup": markup} if markup else {}
        path = output_dir(self.cfg, "videos", Path(entry.get("output", "")).name)
        if path.is_file() and path.stat().st_size <= VIDEO_LIMIT:
            with open(path, "rb") as fh:
                call(token, "sendVideo", http_timeout=900, files={"video": (path.name, fh, "video/mp4")},
                     chat_id=chat, caption=caption, parse_mode="HTML", supports_streaming=True,
                     width=1080, height=1920, duration=round(entry.get("duration_s") or 0), **extra)
        else:
            size = f"{path.stat().st_size / 2 ** 20:.0f} МБ" if path.is_file() else "файла нет"
            url = self.cfg["notify"].get("panel_url") or ""
            self._send(token, chat, f"{caption}\n\nВидео не отправить ({size}, лимит бота — 50 МБ) — оно в панели {url}",
                       **extra)
        if texts:
            self._send(token, chat, publish_message(texts))
