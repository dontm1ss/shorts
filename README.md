# shorts — контент-ферма коротких видео

Конвейер «от сырых данных до готового mp4» для трёх форматов вертикальных видео (1080×1920):

- **iMessage-драма** — сценарий переписки (DeepSeek) → озвучка по ролям → запись анимированного
  интерфейса iMessage → паркур-фон.
- **Истории с форумов** — пост → адаптация под 50–60 с (DeepSeek) → озвучка → крупные субтитры
  с пословной подсветкой → паркур-фон.
- **Нарезка клипов** — собственные видео режутся окном 30 с с шагом 2 с → озвучка + субтитры →
  клип сверху, паркур снизу.

Полное ТЗ: [docs/PROJECT_SPEC.md](docs/PROJECT_SPEC.md). План работ: [TODO.md](TODO.md).

## Структура

```
config/
  base.toml      общие настройки (9:16 1080×1920, окно нарезки, 50–60 с, пресеты TTS)
  profiles/      профили железа — выбираются автоматически
docs/            ТЗ и спецификации (PROJECT_SPEC.md, tts-stack-spec.md)
schema/          scene.schema.json — контракт сцены между модулями + валидатор
src/
  core/          конфигурация и определение железа
  tts/           Chatterbox + stressonnx, пресеты подачи
  subtitles/     faster-whisper → SRT/ASS
  scripting/     DeepSeek: сценарии и рерайт историй
  scraper/       парсер форумов + дедупликация
  imessage-ui/   HTML/CSS/JS-интерфейс + запись через Playwright
  video_clipper/ нарезка скользящим окном
  compositor/    раскладка слоёв, субтитры
  renderer/      ffmpeg (NVENC / AMF / x264)
  pipeline/      оркестратор и очередь задач
data/            исходные видео (не в git), processed-forum-ids.json
mock-data/       моковые сцены для изолированной проверки модулей
output/          готовые ролики (не в git)
parkur.mp4       паркур-фон (не в git)
```

## Профили железа

Боевой запуск — на целевом железе из ТЗ; две другие машины — для разработки и тестов.

| Профиль | Машина | TTS | Субтитры | Энкодер |
|---|---|---|---|---|
| `win10_nvidia_gtx1060` | **целевое:** Win 10, GTX 1060 6 ГБ, Core i3 | cuda | cuda / int8 | h264_nvenc |
| `win11_amd` | Win 11, RX 6600, Ryzen 5 5600X | cpu | cpu / int8 | h264_amf |
| `mac_apple_silicon` | macOS, M-серия | mps | cpu / int8 | h264_videotoolbox |

Профиль выбирается автоматически по ОС и GPU. Проверить, что выбрано на текущей машине
(заодно реально проверяется, работает ли энкодер):

```bash
py -3.11 src/core/config.py
```

Задать вручную — `SHORTS_PROFILE=<имя>` в `.env` или в окружении. В коде:
`from core.config import load_config; cfg = load_config()`.

## Настройка

1. Скопировать `.env.example` в `.env` и вписать `DEEPSEEK_API_KEY`.
2. Положить `parkur.mp4` в корень репозитория.
3. Проверить профиль: `py -3.11 src/core/config.py`.

## Проверки

```bash
py -3.11 schema/validate_scene.py mock-data/*.json
py -3.11 -m pytest tests
```
