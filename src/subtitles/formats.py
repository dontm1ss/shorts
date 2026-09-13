"""Группировка слов в титры и запись SRT / ASS (ASS — с пословной подсветкой и «прыжком»)."""
from __future__ import annotations

from pathlib import Path

from subtitles.align import Word

_SENTENCE_END = (".", "!", "?", "…")


def group_words(words: list[Word], max_words: int, max_chars: int, max_gap_s: float) -> list[list[Word]]:
    """1–max_words слов на экране; новая группа — на конце предложения, длинной паузе
    и смене реплики."""
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in words:
        if current and (
            len(current) >= max_words
            or sum(len(w.text) + 1 for w in current) + len(word.text) > max_chars
            or word.start - current[-1].end > max_gap_s
            or word.line != current[-1].line
        ):
            groups.append(current)
            current = []
        current.append(word)
        if word.text.endswith(_SENTENCE_END):
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _group_end(groups: list[list[Word]], i: int, max_hold_s: float = 0.5) -> float:
    """Титр держится до начала следующего, но не дольше max_hold_s после последнего слова."""
    end = groups[i][-1].end
    if i + 1 < len(groups):
        return min(max(end, groups[i + 1][0].start), end + max_hold_s)
    return end + max_hold_s


def _srt_time(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def _ass_time(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def _ass_color(hex_rgb: str) -> str:
    r, g, b = hex_rgb.lstrip("#")[0:2], hex_rgb.lstrip("#")[2:4], hex_rgb.lstrip("#")[4:6]
    return f"&H00{b}{g}{r}".upper()


def _display(word: Word, uppercase: bool) -> str:
    text = word.text.replace("{", "(").replace("}", ")")
    return text.upper() if uppercase else text


def write_srt(groups: list[list[Word]], path: Path, uppercase: bool = False) -> Path:
    lines = []
    for i, group in enumerate(groups):
        lines += [str(i + 1), f"{_srt_time(group[0].start)} --> {_srt_time(_group_end(groups, i))}",
                  " ".join(_display(w, uppercase) for w in group), ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_ass(groups: list[list[Word]], path: Path, sub_cfg: dict, layout: str,
              width: int = 1080, height: int = 1920) -> Path:
    st = sub_cfg["style"]
    y = sub_cfg["position_y"][layout]
    pop = st["pop_scale"]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{st['font']},{st['size']},{_ass_color(st['color'])},{_ass_color(st['highlight_color'])},{_ass_color(st['outline_color'])},&H80000000,-1,0,0,0,100,100,0,0,1,{st['outline']},{st['shadow']},5,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hl = _ass_color(st["highlight_color"])
    events = []
    for gi, group in enumerate(groups):
        group_end = _group_end(groups, gi)
        for k, word in enumerate(group):
            start = group[0].start if k == 0 else word.start
            end = group[k + 1].start if k + 1 < len(group) else group_end
            if end <= start:
                continue
            parts = []
            for j, w in enumerate(group):
                text = _display(w, sub_cfg["uppercase"])
                if j == k:
                    text = f"{{\\1c{hl}\\t(0,80,\\fscx{pop}\\fscy{pop})}}{text}{{\\r}}"
                parts.append(text)
            # появление группы — короткий fade, дальше только смена подсветки
            fade = "\\fad(60,0)" if k == 0 else ""
            events.append(f"Dialogue: 0,{_ass_time(start)},{_ass_time(end)},Default,,0,0,0,,"
                          f"{{\\an5\\pos({width // 2},{y}){fade}}}{' '.join(parts)}")
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path
