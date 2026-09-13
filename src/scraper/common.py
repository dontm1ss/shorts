"""Общее для парсеров форумов: извлечение текста из HTML, технические фильтры постов.

Все источники приводят посты к одному формату (id, source, title, text, url, score,
over_18, is_self, stickied, published), поэтому фильтр и разбор HTML — один на всех.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

_BLOCK_TAGS = {"p", "br", "li", "blockquote", "h1", "h2", "h3", "h4", "pre", "tr", "hr"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n\n" if tag != "li" else "\n- ")

    def handle_data(self, data):
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    parser = _TextExtractor()
    parser.feed(fragment)
    text = "".join(parser.parts)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*", "\n\n", text).strip()


def reject_reason(post: dict, sc: dict) -> str | None:
    """Технические фильтры (ТЗ 2.4): есть текст, не удалён/закреплён, длина, NSFW.
    По лексике не фильтруем."""
    text = post["text"].strip()
    if not post.get("is_self", True):
        return "не текстовый"
    if post.get("stickied"):
        return "закреплён"
    if text in ("", "[removed]", "[deleted]"):
        return "нет текста"
    if not sc["min_chars"] <= len(text) <= sc["max_chars"]:
        return "длина"
    if sc["exclude_nsfw"] and post["over_18"]:
        return "nsfw"
    return None
