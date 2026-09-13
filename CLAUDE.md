# Инструкции для агента

## Порядок работы
- План и статусы — в `TODO.md`. Режим (с 2026-09-13): задачи выполняются подряд без
  ожидания подтверждения; вопросы к владельцу собирать и задавать по ходу, пока ответа нет —
  работать с разумными значениями по умолчанию и помечать их в TODO. Коммит — по задаче.
- ТЗ: `docs/PROJECT_SPEC.md`, стек TTS: `docs/tts-stack-spec.md` (следовать дословно).

## Железо и профили
- Боевой запуск — **Windows 10 + GTX 1060 6 ГБ + Core i3** (профиль `win10_nvidia_gtx1060`).
  Решения по производительности принимать под это железо.
- Разработка/тесты идут на других машинах: `win11_amd` (RX 6600 + Ryzen 5600X),
  `mac_apple_silicon`. Скорость на них **не** показательна для целевого железа.
- В начале сессии запустить `venv\Scripts\python src\core\config.py` — показывает выбранный
  профиль и реально проверяет энкодер. Нет venv — `scripts\setup.ps1` / `scripts/setup.sh`. Не хардкодить `cuda` / `h264_nvenc`: брать device, encoder,
  compute_type из `load_config()`.
- Код, специфичный для CUDA/NVENC, должен иметь рабочий путь и на dev-профилях
  (cpu/mps, amf/videotoolbox, запасной libx264).

## Правила конвейера
- Тяжёлые задачи (TTS, whisper, рендер) — строго последовательно (`queue.max_parallel_jobs = 1`).
- Формат видео — 9:16, 1080×1920.
- Секреты только в `.env` (в git не попадает). Не выводить ключи в логи.
- Скрипт TTS нельзя называть `chatterbox.py`.

## Окружение
- Всё запускать из `venv` (Python 3.11). torch — только 2.6.0 с индекса профиля
  (`install.torch_index_url`); не ставить torch с PyPI на Windows — там CPU-сборка.
- Модели HuggingFace кэшируются в `%USERPROFILE%\.cache\huggingface` (не в репозитории).

## Проверки
```
venv\Scripts\python scripts\check_env.py        # чек-лист tts-stack-spec, раздел 11
venv\Scripts\python scripts\tts_smoke_test.py   # синтез + whisper на устройстве профиля
venv\Scripts\python -m pytest
```
