import re

import pytest

from subtitles import Word, align_line, group_words, proportional, script_words, write_ass, write_srt
from subtitles.align import normalize
from subtitles.formats import _ass_color, _ass_time, _srt_time


def asr(*items):
    return [Word(t, s, e) for t, s, e in items]


def test_normalize():
    assert normalize("Стари\u0301нный,") == "старинный"
    assert normalize("ЁЛКА!") == "елка"
    assert normalize("кто-то") == "кто-то"


def test_proportional_covers_window():
    words = proportional(["а", "длинное", "слово"], 1.0, 3.0)
    assert words[0].start == 1.0 and words[-1].end == pytest.approx(3.0)
    assert words[1].end - words[1].start > words[0].end - words[0].start   # длиннее слово — дольше


def test_align_exact_match_takes_asr_times():
    line = ["Ты", "правда", "думала?"]
    recognized = asr(("ты", 0.1, 0.3), ("правда", 0.35, 0.8), ("думала", 0.85, 1.4))
    words = align_line(line, recognized, 0.0, 1.5)
    assert [w.text for w in words] == line                       # текст — из сценария
    assert [(w.start, w.end) for w in words] == [(0.1, 0.3), (0.35, 0.8), (0.85, 1.4)]


def test_align_misrecognized_and_missing_words():
    # «узнаю» распознано как «узнал», «не» пропущено whisper'ом
    line = ["что", "я", "не", "узнаю"]
    recognized = asr(("что", 1.0, 1.2), ("я", 1.25, 1.35), ("узнал", 1.6, 2.1))
    words = align_line(line, recognized, 1.0, 2.2)
    assert [w.text for w in words] == line
    assert words[2].start >= words[1].end and words[2].end <= words[3].start   # «не» — между соседями
    assert all(1.0 <= w.start < w.end <= 2.2 for w in words)
    assert all(a.end <= b.start + 1e-9 for a, b in zip(words, words[1:]))     # без наложений


def test_align_ignores_words_of_other_lines():
    recognized = asr(("чужое", 5.0, 5.5), ("слово", 5.5, 6.0))
    words = align_line(["своё", "слово"], recognized, 0.0, 1.0)
    assert words[-1].end <= 1.0                                   # пропорционально в своём окне


def test_script_words_without_whisper():
    scene = {"script": [
        {"speaker": "A", "text": "Привет, как дела?", "timestamp_ms": 0, "duration_ms": 1500},
        {"speaker": "B", "text": "Нормально.", "timestamp_ms": 1850, "duration_ms": 800},
    ]}
    words = script_words(scene, None)
    assert [w.line for w in words] == [0, 0, 0, 1]
    assert words[3].start == pytest.approx(1.85) and words[3].end == pytest.approx(2.65)


def test_script_words_requires_tts():
    with pytest.raises(ValueError, match="озвучьте"):
        script_words({"script": [{"speaker": "A", "text": "Текст"}]}, None)


def test_grouping_rules():
    words = [
        Word("Мой", 0.0, 0.2), Word("сосед", 0.2, 0.6), Word("три", 0.6, 0.8), Word("года", 0.8, 1.1),
        Word("парковался.", 1.1, 1.8),                                  # конец предложения
        Word("Я", 1.9, 2.0), Word("терпел", 3.0, 3.4),                  # пауза > 0.6 с
        Word("Нет!", 3.5, 3.8, line=1),                                  # новая реплика
    ]
    groups = group_words(words, max_words=3, max_chars=16, max_gap_s=0.6)
    assert [[w.text for w in g] for g in groups] == [
        ["Мой", "сосед", "три"], ["года", "парковался."], ["Я"], ["терпел"], ["Нет!"]]


def test_max_chars():
    words = [Word("превосходнейший", 0, 1), Word("день", 1, 2)]
    assert len(group_words(words, max_words=3, max_chars=16, max_gap_s=1)) == 2


def test_time_formats():
    assert _srt_time(3723.456) == "01:02:03,456"
    assert _ass_time(3723.456) == "1:02:03.46"
    assert _ass_color("#FFE600") == "&H0000E6FF"     # ASS: AABBGGRR


def test_srt_and_ass(tmp_path, dev_cfg):
    groups = [[Word("Привет,", 0.0, 0.4), Word("мир!", 0.5, 0.9)], [Word("Пока", 1.5, 1.9)]]
    srt = write_srt(groups, tmp_path / "s.srt", uppercase=True).read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:01,400" in srt       # держится до следующего титра (≤ +0.5 с)
    assert "ПРИВЕТ МИР\n" in srt                         # без знаков препинания

    ass = write_ass(groups, tmp_path / "s.ass", dev_cfg["subtitles"], "top_bottom").read_text(encoding="utf-8")
    events = [line for line in ass.splitlines() if line.startswith("Dialogue:")]
    assert len(events) == 3                               # по событию на каждое подсвеченное слово
    assert "\\pos(540,960)" in events[0]                  # стык клипа и паркура
    assert re.search(r"\\1c&H0000E6FF[^}]*}ПРИВЕТ\{\\r\} МИР$", events[0])
    assert re.search(r"ПРИВЕТ \{\\1c&H0000E6FF[^}]*}МИР\{\\r\}$", events[1])
    assert "Style: Default,Arial Black,92" in ass


def test_display_strips_punctuation(tmp_path):
    groups = [[Word("«Кто-то,", 0.0, 0.4), Word("—", 0.4, 0.5), Word("{сказал}...", 0.5, 0.9)],
              [Word("-", 1.0, 1.1)]]
    srt = write_srt(groups, tmp_path / "s.srt").read_text(encoding="utf-8")
    assert "Кто-то сказал\n" in srt                      # дефис внутри слова остаётся
    assert srt.count("-->") == 1                         # титр из одного тире не показывается
