"""Клиент DeepSeek API (OpenAI-совместимый chat/completions, JSON-режим)."""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class DeepSeekError(RuntimeError):
    pass


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

    def chat_json(self, system: str, user: str, temperature: float | None = None) -> dict:
        """Запрос с ответом-JSON. Повторяет при сетевых ошибках, 429/5xx и битом JSON."""
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
                resp = requests.post(self.url, json=payload, timeout=self.timeout,
                                     headers={"Authorization": f"Bearer {self._key}"})
            except requests.RequestException as e:
                last_error = f"сеть: {type(e).__name__}"
                continue
            if resp.status_code in _RETRY_STATUS:
                last_error = f"HTTP {resp.status_code}"
                continue
            if resp.status_code != 200:
                raise DeepSeekError(f"HTTP {resp.status_code}: {resp.text[:300]}")
            content = resp.json()["choices"][0]["message"]["content"]
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                last_error = "ответ не JSON"
                continue
        raise DeepSeekError(f"DeepSeek не ответил после {self.max_retries} попыток ({last_error})")
