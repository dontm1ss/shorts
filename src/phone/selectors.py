"""UI-селекторы приложений для публикации роликов с телефона.

Селекторы живут здесь одним местом, чтобы их было легко править под конкретную версию
приложения, не трогая код постинга. Смотреть реальные элементы — через weditor
(`python -m weditor` из venv) или `python -m phone screenshot`.

Формат: каждому шагу соответствует список вариантов, перебираемых по порядку. Вариант —
словарь с одним из ключей:

    text           — точный текст элемента (d(text=...))
    text_contains  — вхождение текста (d(textContains=...))
    desc           — contentDescription (d(description=...))
    resource_id    — d(resourceId=...)
    xpath          — d.xpath(...).get(0)
    coord          — доля экрана [x, y] (0..1) — запасной тап по координатам

Интерфейсы приложений меняются между версиями и регионами — селекторы ниже рабочие
отправные точки, а не гарантия. При обновлении приложения чинить их здесь.
"""
from __future__ import annotations

SELECTORS: dict[str, dict[str, list[dict]]] = {
    "tiktok": {
        # «+» внизу по центру → экран создания
        "create": [
            {"desc": "Create"},
            {"desc_contains": "Create"},
            {"resource_id": "com.zhiliaoapp.musically:id/e4g"},
            {"coord": [0.5, 0.93]},
        ],
        # «Upload» / «Загрузить» — выбрать ролик из галереи
        "upload": [
            {"text": "Upload"},
            {"text_contains": "Upload"},
            {"desc": "Upload"},
            {"coord": [0.5, 0.8]},
        ],
        # первый элемент галереи (только что залитый ролик — самый свежий)
        "gallery_first": [
            {"xpath": '//*[@clickable="true"]/android.widget.ImageView[1]'},
            {"coord": [0.18, 0.16]},
        ],
        "next": [
            {"text": "Next"},
            {"desc": "Next"},
            {"coord": [0.9, 0.93]},
        ],
        # второй экран (после обрезки) — там же «Next»/«Post»
        "next2": [
            {"text": "Next"},
            {"desc": "Next"},
            {"coord": [0.9, 0.93]},
        ],
        # поле подписи
        "caption": [
            {"resource_id": "com.zhiliaoapp.musically:id/e62"},
            {"desc": "Add caption"},
            {"coord": [0.5, 0.3]},
        ],
        # «Post» → может быть подтверждение вторым «Post»
        "post": [
            {"text": "Post"},
            {"desc": "Post"},
            {"coord": [0.9, 0.93]},
        ],
        "post_confirm": [
            {"text": "Post"},
            {"desc": "Post"},
            {"coord": [0.9, 0.93]},
        ],
    },
    "youtube": {
        # «+» внизу по центру
        "create": [
            {"desc": "Create"},
            {"desc_contains": "Create"},
            {"coord": [0.5, 0.95]},
        ],
        "upload_video": [
            {"text": "Upload video"},
            {"text_contains": "Upload a video"},
            {"text_contains": "Upload"},
            {"coord": [0.5, 0.35]},
        ],
        # выбор ролика из системного файлового пикера
        "select": [
            {"desc": "Show roots"},
            {"coord": [0.18, 0.16]},
        ],
        "next": [
            {"text": "Next"},
            {"desc": "Next"},
            {"coord": [0.9, 0.95]},
        ],
        "title": [
            {"text": "Add title"},
            {"desc": "Title"},
            {"coord": [0.5, 0.3]},
        ],
        "next2": [
            {"text": "Next"},
            {"coord": [0.9, 0.95]},
        ],
        "publish": [
            {"text": "Upload Short"},
            {"text": "Upload"},
            {"coord": [0.9, 0.95]},
        ],
    },
    "instagram": {
        # «+» внизу по центру
        "create": [
            {"desc": "New post"},
            {"desc_contains": "Create"},
            {"coord": [0.5, 0.96]},
        ],
        "reel": [
            {"text": "Reel"},
            {"desc": "Reel"},
            {"coord": [0.5, 0.62]},
        ],
        "gallery_first": [
            {"coord": [0.18, 0.16]},
        ],
        "next": [
            {"text": "Next"},
            {"desc": "Next"},
            {"coord": [0.9, 0.95]},
        ],
        "next2": [
            {"text": "Next"},
            {"desc": "Next"},
            {"coord": [0.9, 0.95]},
        ],
        "caption": [
            {"text": "Write a caption..."},
            {"desc": "Write a caption"},
            {"coord": [0.5, 0.82]},
        ],
        "share": [
            {"text": "Share"},
            {"desc": "Share"},
            {"coord": [0.9, 0.95]},
        ],
    },
}


def flow(platform: str) -> list[str]:
    """Порядок шагов постинга для площадки (имена — ключи SELECTORS[platform])."""
    if platform == "tiktok":
        return ["create", "upload", "gallery_first", "next", "next2", "caption", "post", "post_confirm"]
    if platform == "youtube":
        return ["create", "upload_video", "select", "next", "title", "next2", "publish"]
    if platform == "instagram":
        return ["create", "reel", "gallery_first", "next", "next2", "caption", "share"]
    raise KeyError(platform)


# Прогрев: открыть поиск, ввести ключевик и открыть первый результат — дальше идёт лента
# с похожим контентом. youtube/instagram без поиска открывают ленты Shorts/Reels.
# Как и SELECTORS, это отправные точки под текущую версию приложения (править по weditor).
WARMUP: dict[str, dict[str, list[dict]]] = {
    "tiktok": {
        "search_open": [{"desc": "Search"}, {"desc_contains": "Search"}, {"coord": [0.9, 0.04]}],
        "search_field": [{"resource_id": "com.zhiliaoapp.musically:id/et_search_kw"},
                         {"coord": [0.5, 0.09]}],
        "first_result": [{"xpath": '//*[@clickable="true"]/android.widget.ImageView[1]'},
                         {"coord": [0.25, 0.22]}],
    },
    "youtube": {
        "shorts": [{"desc_contains": "Shorts"}, {"coord": [0.5, 0.95]}],
        "search_open": [{"desc": "Search"}, {"desc_contains": "Search"}, {"coord": [0.95, 0.06]}],
        "search_field": [{"text": "Search YouTube"}, {"coord": [0.5, 0.09]}],
        "first_result": [{"coord": [0.5, 0.35]}],
    },
    "instagram": {
        "reels": [{"desc": "Reels"}, {"desc_contains": "Reels"}, {"coord": [0.3, 0.95]}],
        "search_open": [{"desc": "Search and Explore"}, {"desc_contains": "Search"},
                        {"coord": [0.9, 0.95]}],
        "search_field": [{"text": "Search"}, {"coord": [0.5, 0.09]}],
        "first_result": [{"coord": [0.25, 0.3]}],
    },
}

