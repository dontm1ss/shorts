"""Ускорения конвейера:
1) клипы — один проход whisper на весь исходник, отрезки берут свои слова из него;
2) субтитры озвученной сцены — таймкоды из проверки озвучки, без второго whisper;
3) озвучка пачкой — модель грузится один раз на все готовые задачи tts_synthesis."""
import json

import numpy as np
import pytest
import soundfile as sf

import pipeline.jobs
import pipeline.worker
import subtitles.scene
from core.media import run_ffmpeg
from core.scene import scene_errors
from pipeline.queue import JobQueue
from subtitles import Word, clip_words, load_words, save_words, subtitle_scene, transcribe_source
from test_tts import SR, FakeEngine, mock_scene
from tts.scene import stretch_scene_audio, synthesize_scene


@pytest.fixture
def no_whisper(monkeypatch):
    """Любой вызов whisper в модуле субтитров — ошибка теста."""
    def fail(*a, **kw):
        raise AssertionError("whisper не должен вызываться")
    monkeypatch.setattr(subtitles.scene, "transcribe", fail)


# --- 2. таймкоды из проверки озвучки ------------------------------------------------

def test_tts_saves_recognized_words(dev_cfg, tmp_path):
    voiced = synthesize_scene(mock_scene("imessage_drama"), FakeEngine(dev_cfg), out_path=tmp_path / "v.wav")
    assert scene_errors(voiced) == []
    words = load_words(tmp_path / "v.words.json")
    assert voiced["audio"]["words_path"].endswith("v.words.json")
    assert [w.line for w in words] == sorted(w.line for w in words)
    for line_no, line in enumerate(voiced["script"]):
        mine = [w for w in words if w.line == line_no]
        assert [w.text for w in mine] == line["stressed_text"].split()       # FakeEngine узнаёт текст как есть
        t0, t1 = line["timestamp_ms"] / 1000, (line["timestamp_ms"] + line["duration_ms"]) / 1000
        assert all(t0 - 0.01 <= w.start < w.end <= t1 + 0.01 for w in mine)  # слова внутри своей реплики
    assert all(a.end <= b.start + 1e-6 for a, b in zip(words, words[1:]))


def test_no_words_without_verification(dev_cfg, tmp_path):
    dev_cfg["tts"]["verify"] = False
    voiced = synthesize_scene(mock_scene("reddit_story"), FakeEngine(dev_cfg), out_path=tmp_path / "v.wav")
    assert "words_path" not in voiced["audio"]


def test_stretch_rescales_words(dev_cfg, tmp_path):
    voiced = synthesize_scene(mock_scene("reddit_story"), FakeEngine(dev_cfg), out_path=tmp_path / "v.wav")
    before = load_words(tmp_path / "v.words.json")
    target = round(voiced["audio"]["duration_ms"] * 0.95)            # ускорить на ~5%
    stretched = stretch_scene_audio(voiced, target)
    after = load_words(tmp_path / "v.words.json")
    rate = voiced["audio"]["duration_ms"] / target
    assert stretched is not None
    assert [w.end for w in after] == pytest.approx([w.end / rate for w in before])


def test_subtitles_reuse_tts_words(dev_cfg, tmp_path, no_whisper):
    voiced = synthesize_scene(mock_scene("reddit_story"), FakeEngine(dev_cfg), out_path=tmp_path / "v.wav")
    result = subtitle_scene(voiced, dev_cfg, out_dir=tmp_path / "subs")
    words = json.loads((tmp_path / "subs" / "words.json").read_text(encoding="utf-8"))
    tts_words = load_words(tmp_path / "v.words.json")
    assert len(words) == len(tts_words)
    assert [w["start"] for w in words] == pytest.approx([w.start for w in tts_words])   # время — из проверки
    assert result["subtitles"]["path"].endswith("subs.ass")


# --- 1. один whisper на исходник ---------------------------------------------------

def test_clip_words():
    words = [Word("раз", 0.5, 0.9), Word("два", 1.8, 2.3), Word("три", 2.9, 3.4), Word("четыре", 4.0, 4.6)]
    got = clip_words(words, 2.0, 4.0)
    assert [(w.text, round(w.start, 2), round(w.end, 2)) for w in got] == [("два", 0.0, 0.3), ("три", 0.9, 1.4)]


def test_clip_subtitles_from_source_words(dev_cfg, tmp_path, no_whisper):
    save_words([Word("привет", 30.2, 30.6), Word("мир", 31.0, 31.4), Word("чужое", 70.0, 70.5)],
               tmp_path / "words.json")
    scene = {"scene_id": "c5d9e7b2-4a1f-4c8d-8e2b-7f3a1b6c0d55", "type": "video_clip",
             "created_at": "2026-09-13T12:10:00Z",
             "source_video": {"path": "x.mp4", "clip_start_ms": 30000, "clip_end_ms": 60000,
                              "words_path": str(tmp_path / "words.json")},
             "output": {"resolution": "1080x1920", "path": "output/videos/x.mp4"}}
    subtitle_scene(scene, dev_cfg, out_dir=tmp_path / "subs")
    words = json.loads((tmp_path / "subs" / "words.json").read_text(encoding="utf-8"))
    assert [w["text"] for w in words] == ["привет", "мир"]
    assert words[0]["start"] == pytest.approx(0.2)


def test_transcribe_source_once(dev_cfg, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subtitles.scene, "transcribe",
                        lambda wav, cfg, language=None: calls.append(wav) or [Word("слово", 1.0, 1.5)])
    with_audio, silent = tmp_path / "a.mp4", tmp_path / "s.mp4"
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=320x240:d=3", "-f", "lavfi", "-i", "sine=d=3",
                "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", str(with_audio)])
    run_ffmpeg(["-f", "lavfi", "-i", "testsrc=s=320x240:d=3", "-c:v", "libx264", "-preset", "ultrafast", str(silent)])

    out = transcribe_source(with_audio, dev_cfg, tmp_path / "w1.json")
    assert [w.text for w in load_words(out)] == ["слово"] and len(calls) == 1
    assert not out.with_suffix(".wav").exists()                          # временный wav убран
    assert load_words(transcribe_source(silent, dev_cfg, tmp_path / "w2.json")) == []
    assert len(calls) == 1                                               # без звука whisper не зовётся


# --- 3. озвучка пачкой -------------------------------------------------------------

@pytest.fixture
def queue_cfg(dev_cfg, tmp_path):
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    return dev_cfg


def test_claim_ready_takes_only_ready_jobs_of_kind(tmp_path):
    q = JobQueue(tmp_path / "q.sqlite")
    first = q.enqueue("tts_synthesis", {"n": 1})
    q.enqueue("tts_synthesis", {"n": 2})
    blocked_parent = q.enqueue("script_story", {})
    q.enqueue("tts_synthesis", {"n": 3}, depends_on=blocked_parent)   # ещё не готова
    q.enqueue("video_render", {})
    assert q.claim_next().id == first
    assert [j.payload["n"] for j in q.claim_ready("tts_synthesis", 10)] == [2]
    assert q.claim_ready("tts_synthesis", 10) == []
    q.close()


def test_worker_batches_ready_tts_jobs(queue_cfg, monkeypatch):
    queue_cfg["queue"]["tts_batch_size"] = 20
    q = pipeline.worker.open_queue(queue_cfg)
    for n in range(3):
        q.enqueue_chain([{"kind": "tts_synthesis", "payload": {"n": n}},
                         {"kind": "subtitle_generation", "payload": {"n": n}}])
    q.close()
    batches = []

    def fake_run(jobs, cfg):
        batches.append([(j.kind, j.payload["n"]) for j in jobs])
        return {j.id: (True, {}, "") for j in jobs}

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", fake_run)
    counts = pipeline.worker.run_worker(queue_cfg)
    assert batches[0] == [("tts_synthesis", 0), ("tts_synthesis", 1), ("tts_synthesis", 2)]
    assert sorted(batches[1:]) == [[("subtitle_generation", n)] for n in range(3)]
    assert counts == {"done": 6}


def test_run_jobs_shares_context_and_isolates_failures(queue_cfg, monkeypatch):
    seen, closed = [], []

    def handler(payload, cfg, ctx):
        seen.append(id(ctx))
        if payload["n"] == 1:
            raise RuntimeError("сбой второй задачи")
        return {"n": payload["n"]}

    monkeypatch.setitem(pipeline.jobs.HANDLERS, "tts_synthesis", handler)
    monkeypatch.setattr(pipeline.jobs.JobContext, "close", lambda self: closed.append(True))
    q = pipeline.worker.open_queue(queue_cfg)
    ids = [q.enqueue("tts_synthesis", {"n": n}) for n in range(3)]
    jobs = [q.get(i) for i in ids]
    q.close()

    assert pipeline.worker.run_jobs(ids, queue_cfg) == 1
    assert len(set(seen)) == 1 and len(seen) == 3                       # один контекст на всю пачку
    assert closed == [True]
    results = [pipeline.worker.job_files(queue_cfg, j)[1] for j in jobs]
    assert results[0].exists() and not results[1].exists() and results[2].exists()
    assert "сбой второй задачи" in pipeline.worker._error_file(queue_cfg, jobs[1]).read_text(encoding="utf-8")


def test_context_loads_tts_engine_once(dev_cfg):
    ctx = pipeline.jobs.JobContext(dev_cfg)
    assert ctx.tts_engine() is ctx.tts_engine()
