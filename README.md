# shorts — контент-ферма коротких видео

Конвейер «от сырых данных до готового mp4» для трёх форматов вертикальных видео (9:16, 1080×1920):

- **Истории с форумов** — пост Reddit → адаптация на русский под 50–60 с (DeepSeek) → озвучка
  Chatterbox → крупные субтитры с пословной подсветкой → паркур на весь кадр.
- **iMessage-драма** — переписка по ролям (DeepSeek) → озвучка → покадровая запись переписки
  в стиле iMessage (только сообщения: пузыри, «печатает…», «Прочитано») → переписка сверху, паркур снизу.
- **Нарезка клипов** — собственные видео режутся окном 30 с с шагом 2 с → субтитры по звуку →
  клип сверху, паркур снизу.

Полное ТЗ: [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md), стек TTS: [docs/tts-stack-spec.md](docs/tts-stack-spec.md).
План и статусы: [TODO.md](TODO.md).

## Быстрый старт

```bash
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

Затем `DEEPSEEK_API_KEY` в `.env` (шаблон — `.env.example`) и `parkur.mp4` в корень репозитория —
это фон по умолчанию из пула `[[backgrounds]]` в `config/base.toml`; туда же добавляются
дополнительные фоновые видео (выбор случайный на каждую сцену).
Источники историй: Pikabu — без ключей (общая RSS-лента); Reddit — без ключей тоже работает
(RSS с жёстким лимитом запросов), для официального API нужны ключи приложения типа «script»
(`REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` в `.env`).

```bash
venv\Scripts\python -m pipeline story --scrape 1
```

Готовые ролики — в `output/videos/`.

## Веб-панель отбора сценариев

Сценарий после DeepSeek не идёт в озвучку сразу, а ждёт решения в панели: прочитать, поправить
текст, одобрить (уходит в озвучку и рендер) или отклонить. Там же — кнопки «Сгенерировать
N историй / переписок», готовые ролики с плеером и очередь задач. Отбор выключается
`review.enabled = false` в `config/base.toml`.

```bash
venv\Scripts\python -m panel                                     # http://127.0.0.1:8765
powershell -ExecutionPolicy Bypass -File scripts\panel_online.ps1 # то же + доступ из интернета через сервер
```

Доступ из интернета — HTTPS на сервере + SSH-туннель к ПК, пароль `PANEL_PASSWORD` в `.env`:
[docs/panel-deploy.md](docs/panel-deploy.md).

## Конвейер

Всё идёт через очередь задач на SQLite (`output/queue.sqlite`): тяжёлые этапы строго по одному,
каждый — отдельным процессом. Команды ставят задачи в очередь и сразу запускают воркер
(`--no-run` — только поставить).

```bash
venv\Scripts\python -m pipeline story --scrape 3                 # спарсить Reddit + Pikabu, взять 3 лучших поста
venv\Scripts\python -m pipeline story --post data\forum-posts\reddit_xxx.json
venv\Scripts\python -m pipeline drama --topic "подруга слила переписку" --count 2
venv\Scripts\python -m pipeline clips data\source-videos\film.mp4 --limit 5
venv\Scripts\python -m pipeline worker [--watch]                 # выполнить очередь
venv\Scripts\python -m pipeline status                           # состояние задач
venv\Scripts\python -m pipeline retry 12                         # повторить упавшую задачу
```

| Задача | Что делает |
|---|---|
| `script_batch` | N историй: лучшие неиспользованные посты, при нехватке — сначала парсинг форумов |
| `script_story` | DeepSeek адаптирует пост → на отбор в панель (или сразу в работу без отбора) |
| `script_drama` | DeepSeek пишет переписку A/B |
| `tts_synthesis` | озвучка; все готовые задачи — пачкой в одном процессе (модель грузится один раз) |
| `subtitle_generation` | таймкоды из проверки озвучки (клипы — из одного распознавания исходника) → ASS/SRT; шрифт и цвет подсветки свои у каждой сцены |
| `ui_render` | покадровая запись интерфейса iMessage (Playwright + системный Edge/Chrome) |
| `video_render` | случайный отрезок фона + раскладка + субтитры → ffmpeg (энкодер профиля); после — удаление тяжёлых промежуточных файлов |
| `video_clip_extraction` | нарезка окном 30/2 с, сцены и цепочки задач на каждый сегмент |

Логи задач — `output/logs/`, состояние каждой сцены — `output/scenes/<scene_id>.json`,
промежуточные файлы — `output/work/<scene_id>/`. Список всех готовых роликов (заголовок,
формат, источник, длительность, путь) — `output/manifest.json`, пополняется после каждого
`video_render`.

## Модули по отдельности

Каждый модуль запускается сам по себе на сцене из `output/scenes/` или `tests/fixtures/`:

```bash
venv\Scripts\python -m tts scene tests\fixtures\scene.reddit_story.json  # озвучка сцены
venv\Scripts\python -m tts presets "Текст"                             # сравнить пресеты на слух
venv\Scripts\python -m subtitles output\scenes\<id>.json                # субтитры
venv\Scripts\python -m imessage --preview output\scenes\<id>.json       # кадр интерфейса iMessage
venv\Scripts\python -m imessage output\scenes\<id>.json                 # видео интерфейса
venv\Scripts\python -m video_clipper data\source-videos                 # нарезка папки
venv\Scripts\python -m scraper --sub tifu                               # новые посты Reddit + Pikabu в data\forum-posts
```

## Структура

```
config/
  base.toml          общие настройки: формат, хронометраж, пресеты TTS, стиль субтитров, раскладки
  profiles/          профили железа — выбираются автоматически
docs/                ТЗ и спецификации
schema/              scene.schema.json — контракт сцены между модулями + валидатор
scripts/             установка окружения, чек-лист, смок-тест TTS
src/
  core/              конфиг и определение железа, сцена, ffmpeg/ffprobe
  tts/               Chatterbox + stressonnx, проверка кусков whisper'ом, синтез сцены по ролям
  subtitles/         faster-whisper, выравнивание по сценарию, ASS с подсветкой слов
  scripting/         DeepSeek: истории, переписки, подгонка длины, генератор параметров разнообразия
  scraper/           Reddit (RSS/API) + Pikabu (RSS), дедупликация
  imessage/          web/ — копия интерфейса iMessage; запись кадров через Playwright
  video_clipper/     нарезка скользящим окном
  compositor/        пул фоновых видео, раскладки кадра (сборка команды ffmpeg)
  renderer/          финальный рендер (энкодер профиля, запасной libx264)
  pipeline/          очередь SQLite, задачи, воркер, CLI
data/                исходные видео (не в git), посты форумов (не в git)
tests/               pytest + tests/fixtures/ — моковые сцены для проверки модулей без полного прогона
output/              всё сгенерированное (не в git)
```

## Профили железа

Боевой запуск — на целевом железе из ТЗ; две другие машины — для разработки и тестов.

| Профиль | Машина | TTS | Субтитры | Энкодер |
|---|---|---|---|---|
| `win10_nvidia_gtx1060` | **целевое:** Win 10, GTX 1060 6 ГБ, Core i3 | cuda | cuda / int8 | h264_nvenc |
| `win11_amd` | Win 11, RX 6600, Ryzen 5 5600X | cpu | cpu / int8 | h264_amf |
| `mac_apple_silicon` | macOS, M-серия | mps | cpu / int8 | h264_videotoolbox |

```bash
venv\Scripts\python src\core\config.py        # какой профиль выбран + реальная проверка энкодера
venv\Scripts\python scripts\check_env.py      # чек-лист окружения
```

Задать профиль вручную — `SHORTS_PROFILE=<имя>` в `.env`.

## Установка

Нужны Python 3.11 и ffmpeg в PATH. Скрипт создаёт `venv`, ставит torch 2.6.0 с индекса профиля
(cu124 / cpu / PyPI), зависимости из `requirements.txt`, пакет проекта и прогоняет чек-лист.

Windows:
```bash
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

macOS:
```bash
bash scripts/setup.sh
```

Первый синтез скачивает веса с HuggingFace: Chatterbox ~3 ГБ, whisper small ~0.5 ГБ, stressonnx ~0.5 ГБ.

## Тесты

```bash
venv\Scripts\python -m pytest
```

Тесты на настоящей модели TTS (минуты на CPU) — `SHORTS_MODEL_TESTS=1`.
