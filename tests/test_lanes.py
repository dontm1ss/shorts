"""Воркер: сценарии DeepSeek — своей линией параллельно тяжёлым задачам; тяжёлые — строго по одной."""
import threading
import time

import pytest

import pipeline.worker


@pytest.fixture
def queue_cfg(dev_cfg, tmp_path):
    dev_cfg["queue"]["db"] = str(tmp_path / "q.sqlite")
    dev_cfg["paths"]["output"] = str(tmp_path / "out")
    return dev_cfg


def enqueue(cfg, *jobs):
    q = pipeline.worker.open_queue(cfg)
    ids = [q.enqueue(kind, payload) for kind, payload in jobs]
    q.close()
    return ids


def test_script_runs_in_parallel_with_heavy_lane(queue_cfg, monkeypatch):
    enqueue(queue_cfg, ("tts_synthesis", {"n": 1}), ("tts_synthesis", {"n": 2}), ("script_drama", {"n": 3}))
    script_done = threading.Event()
    active, peak, script_during_tts = [], [0], []
    lock = threading.Lock()

    def fake_run(jobs, cfg):
        job = jobs[0]
        with lock:
            active.append(job.kind)
            peak[0] = max(peak[0], active.count("tts_synthesis"))
        if job.kind == "script_drama":
            script_done.set()
        elif job.payload["n"] == 1:   # первая озвучка не кончится, пока сценарий не пройдёт параллельно
            script_during_tts.append(script_done.wait(5))
        time.sleep(0.05)
        with lock:
            active.remove(job.kind)
        return {j.id: (True, {}, "") for j in jobs}

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", fake_run)
    counts = pipeline.worker.run_worker(queue_cfg, poll_s=0.05)
    assert counts == {"done": 3}
    assert script_during_tts == [True]                                   # сценарий не ждал тяжёлую линию
    assert peak[0] == 1                                                  # озвучка — строго по одной


def test_chain_from_script_picked_up_by_heavy_lane(queue_cfg, monkeypatch):
    """Автоодобрение: задача сценария ставит цепочку обработки — воркер не выходит, пока её не выполнит."""
    enqueue(queue_cfg, ("script_story", {}))
    ran = []

    def fake_run(jobs, cfg):
        ran.append(jobs[0].kind)
        if jobs[0].kind == "script_story":
            chain = [{"kind": "tts_synthesis", "payload": {}}, {"kind": "video_render", "payload": {}}]
            return {jobs[0].id: (True, {"next": [chain]}, "")}
        return {j.id: (True, {}, "") for j in jobs}

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", fake_run)
    assert pipeline.worker.run_worker(queue_cfg, poll_s=0.05) == {"done": 3}
    assert ran == ["script_story", "tts_synthesis", "video_render"]


def test_single_lane_processing_before_scripts(queue_cfg, monkeypatch):
    queue_cfg["queue"]["light_lane"] = False
    enqueue(queue_cfg, ("script_story", {}), ("script_drama", {}), ("tts_synthesis", {}), ("video_render", {}))
    ran = []

    def fake_run(jobs, cfg):
        ran.append(jobs[0].kind)
        return {j.id: (True, {}, "") for j in jobs}

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", fake_run)
    pipeline.worker.run_worker(queue_cfg, poll_s=0.05)
    assert ran == ["tts_synthesis", "video_render", "script_story", "script_drama"]


def test_one_tts_per_process_by_default(queue_cfg, monkeypatch):
    """tts_batch_size = 1: каждая сцена сразу после озвучки идёт в рендер, а не ждёт озвучку остальных."""
    q = pipeline.worker.open_queue(queue_cfg)
    for n in range(2):
        q.enqueue_chain([{"kind": "tts_synthesis", "payload": {"n": n}},
                         {"kind": "video_render", "payload": {"n": n}}])
    q.close()
    ran = []

    def fake_run(jobs, cfg):
        ran.append([(j.kind, j.payload["n"]) for j in jobs])
        return {j.id: (True, {}, "") for j in jobs}

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", fake_run)
    pipeline.worker.run_worker(queue_cfg, poll_s=0.05)
    assert ran == [[("tts_synthesis", 0)], [("video_render", 0)], [("tts_synthesis", 1)], [("video_render", 1)]]


def test_failed_launch_marks_jobs_failed(queue_cfg, monkeypatch):
    queue_cfg["queue"]["max_attempts"] = 1
    enqueue(queue_cfg, ("script_story", {}))

    def broken(jobs, cfg):
        raise OSError("нет python")

    monkeypatch.setattr(pipeline.worker, "_run_subprocess", broken)
    assert pipeline.worker.run_worker(queue_cfg, poll_s=0.05) == {"failed": 1}
