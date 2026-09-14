"""Клиент DeepSeek API (OpenAI-совместимый chat/completions, JSON-режим).

Ответ читается потоком (stream): при перегрузке DeepSeek не отвечает ошибкой, а держит
соединение пустыми keep-alive строками до 30 минут — обычный таймаут чтения их не замечает,
и задача висела бы бесконечно. Поток показывает, пишет ли модель на самом деле: нет первого
куска текста (ответа или рассуждений — reasoning_content у deepseek-v4-pro) за deepseek.timeout_s —
запрос перезапускается. Начавшийся ответ не обрывается, пока текст идёт.

Модели: основная (deepseek.model, flash); если она не ответила или вернула ошибку — запасная
(deepseek.fallback_model, v4-pro). После сбоя основной следующие fallback_cooldown_s секунд
запросы сразу идут в запасную (отметка в paths.deepseek_state — задачи идут разными процессами),
чтобы каждая задача не ждала минуту зависшую основную."""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from core.config import ROOT

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_FATAL_STATUS = {401, 402}   # ключ или баланс — запасная модель не поможет
_STALL_S = 30       # поток замер (ни байта, даже keep-alive) — обрыв
_TOTAL_S = 300      # предел на весь ответ, даже если текст идёт


class DeepSeekError(RuntimeError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class _Retry(Exception):
    """Попытка не удалась, но можно повторить."""


class DeepSeekClient:
    def __init__(self, cfg: dict):
        d = cfg["deepseek"]
        if not d.get("api_key"):
            raise DeepSeekError("DEEPSEEK_API_KEY не задан — впишите его в .env")
        self._key = d["api_key"]
        self.url = d["base_url"].rstrip("/") + "/chat/completions"
        self.model = d["model"]
        self.fallback_model = d.get("fallback_model") or None
        self.cooldown_s = d.get("fallback_cooldown_s", 600)
        self.temperature = d["temperature"]
        self.timeout = d["timeout_s"]
        self.max_retries = d["max_retries"]
        self._state = ROOT / cfg["paths"]["deepseek_state"]

    # --- какая модель сейчас -----------------------------------------------------

    def _primary_down(self) -> bool:
        """Основная модель недавно не ответила — пока идёт пауза, сразу запасная."""
        try:
            until = json.loads(self._state.read_text(encoding="utf-8")).get("primary_down_until", 0)
        except (OSError, ValueError):
            return False
        return time.time() < until

    def _mark_primary_down(self, reason: str) -> None:
        self._state.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state.with_suffix(".tmp")
        tmp.write_text(json.dumps({"primary_down_until": time.time() + self.cooldown_s, "model": self.model,
                                   "reason": reason}, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._state)

    def _pick_model(self, primary_failed: bool) -> str:
        if self.fallback_model and (primary_failed or self._primary_down()):
            return self.fallback_model
        return self.model

    # --- запрос -------------------------------------------------------------------

    def _request(self, payload: dict) -> str:
        """Один запрос потоком → текст ответа модели."""
        start = time.monotonic()
        parts: list[str] = []
        started = False                  # модель начала писать — ответ или рассуждения
        try:
            with requests.post(self.url, json={**payload, "stream": True}, stream=True,
                               timeout=(15, _STALL_S), headers={"Authorization": f"Bearer {self._key}"}) as resp:
                if resp.status_code in _RETRY_STATUS:
                    raise _Retry(f"HTTP {resp.status_code}")
                if resp.status_code != 200:
                    raise DeepSeekError(f"HTTP {resp.status_code}: {resp.text[:300]}", resp.status_code)
                for line in resp.iter_lines():
                    elapsed = time.monotonic() - start
                    if not started and elapsed > self.timeout:
                        raise _Retry(f"нет ответа за {self.timeout} с (DeepSeek перегружен)")
                    if elapsed > _TOTAL_S:
                        raise _Retry(f"ответ не закончился за {_TOTAL_S} с")
                    if not line.startswith(b"data:"):      # пустые строки и «: keep-alive»
                        continue
                    data = line[5:].strip()
                    if data == b"[DONE]":
                        break
                    for choice in json.loads(data).get("choices", []):
                        delta = choice.get("delta", {})
                        started = started or bool(delta.get("content") or delta.get("reasoning_content"))
                        if delta.get("content"):
                            parts.append(delta["content"])
        except requests.RequestException as e:
            raise _Retry(f"сеть: {type(e).__name__}") from e
        return "".join(parts)

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict:
        """Запрос с ответом-JSON. Повторяет при зависании, сетевых ошибках, 429/5xx и битом JSON;
        сбой основной модели — переход на запасную."""
        payload = {
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature if temperature is None else temperature,
        }
        last_error, primary_failed, switched = "нет попыток", False, False
        for attempt in range(self.max_retries):
            model = self._pick_model(primary_failed)
            if attempt and not switched:             # на запасную сразу после сбоя основной — без паузы
                time.sleep(2 ** attempt)
            switched = False
            start = time.monotonic()
            try:
                result = json.loads(self._request({**payload, "model": model}))
                log.info("DeepSeek: ответ %s за %.0f с", model, time.monotonic() - start)
                return result
            except _Retry as e:
                last_error = str(e)
            except json.JSONDecodeError:
                last_error = "ответ не JSON"
            except DeepSeekError as e:
                if model != self.model or not self.fallback_model or e.status in _FATAL_STATUS:
                    raise
                last_error = str(e)                  # основной модели нет / отказ — на запасную
            if model == self.model and self.fallback_model:
                primary_failed = switched = True
                if last_error != "ответ не JSON":    # один битый ответ — не повод отключать основную
                    self._mark_primary_down(last_error)
                log.warning("DeepSeek %s: %s — переключаюсь на %s", model, last_error, self.fallback_model)
            else:
                log.warning("DeepSeek %s: %s — попытка %d/%d%s", model, last_error, attempt + 1, self.max_retries,
                            ", повторяю" if attempt + 1 < self.max_retries else "")
        raise DeepSeekError(f"DeepSeek не ответил после {self.max_retries} попыток ({last_error})")
