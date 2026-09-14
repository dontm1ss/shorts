"""Клиент DeepSeek API (OpenAI-совместимый chat/completions, JSON-режим).

Ответ читается потоком (stream): при перегрузке DeepSeek не отвечает ошибкой, а держит
соединение пустыми keep-alive строками до 30 минут — обычный таймаут чтения их не замечает,
и задача висела бы бесконечно. Поток показывает, пишет ли модель на самом деле: нет первого
куска текста (ответа или рассуждений — reasoning_content у deepseek-v4-pro) за deepseek.timeout_s —
запрос перезапускается. Начавшийся ответ не обрывается, пока текст идёт."""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}
_STALL_S = 30       # поток замер (ни байта, даже keep-alive) — обрыв
_TOTAL_S = 300      # предел на весь ответ, даже если текст идёт


class DeepSeekError(RuntimeError):
    pass


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
        self.temperature = d["temperature"]
        self.timeout = d["timeout_s"]
        self.max_retries = d["max_retries"]

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
                    raise DeepSeekError(f"HTTP {resp.status_code}: {resp.text[:300]}")
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
        """Запрос с ответом-JSON. Повторяет при зависании, сетевых ошибках, 429/5xx и битом JSON."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": self.temperature if temperature is None else temperature,
        }
        last_error = "нет попыток"
        for attempt in range(self.max_retries):
            if attempt:
                time.sleep(2 ** attempt)
            try:
                content = self._request(payload)
                return json.loads(content)
            except _Retry as e:
                last_error = str(e)
            except json.JSONDecodeError:
                last_error = "ответ не JSON"
            log.warning("DeepSeek: %s — попытка %d/%d%s", last_error, attempt + 1, self.max_retries,
                        ", повторяю" if attempt + 1 < self.max_retries else "")
        raise DeepSeekError(f"DeepSeek не ответил после {self.max_retries} попыток ({last_error})")
