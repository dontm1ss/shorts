"""Воркер очереди: берёт задачи по одной и выполняет в отдельном процессе.

Озвучка — пачкой: все готовые задачи tts_synthesis идут одним процессом друг за другом
(модели TTS и whisper грузятся один раз). Задачи всё равно выполняются строго по одной.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

from core.config import ROOT
from core.scene import output_dir
from pipeline.queue import Job, JobQueue, WorkerLock

log = logging.getLogger(__name__)

BATCH_KINDS = {"tts_synthesis"}


def open_queue(cfg: dict) -> JobQueue:
    return JobQueue(ROOT / cfg["queue"]["db"], cfg["queue"]["max_attempts"])


def job_files(cfg: dict, job: Job) -> tuple[Path, Path]:
    logs = output_dir(cfg, "logs")
    return logs / f"job_{job.id:05d}_{job.kind}.log", logs / f"job_{job.id:05d}.result.json"


def _error_file(cfg: dict, job: Job) -> Path:
    return job_files(cfg, job)[1].with_name(f"job_{job.id:05d}.error.txt")


def _run_subprocess(jobs: list[Job], cfg: dict) -> dict[int, tuple[bool, dict, str]]:
    """Один процесс на пачку задач. Итог каждой — по её result.json / error.txt."""
    log_path = job_files(cfg, jobs[0])[0]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    for job in jobs:
        job_files(cfg, job)[1].unlink(missing_ok=True)
        _error_file(cfg, job).unlink(missing_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "SHORTS_PROFILE": cfg["profile"]["name"]}
    cmd = [sys.executable, "-m", "pipeline", "job", *(str(j.id) for j in jobs)]
    tail: list[str] = []
    with open(log_path, "a", encoding="utf-8") as fh:
        batch = f", пачка {', '.join(f'#{j.id}' for j in jobs)}" if len(jobs) > 1 else ""
        fh.write(f"\n=== попытка {jobs[0].attempts}{batch}, {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            fh.write(line)
            line = line.rstrip()
            if line and "\r" not in line and not line.startswith(("Sampling", "Fetching")):
                print(f"    │ {line}", flush=True)
            tail = (tail + [line])[-20:]
        code = proc.wait()

    outcomes = {}
    for job in jobs:
        result_path, error_path = job_files(cfg, job)[1], _error_file(cfg, job)
        if result_path.exists():
            outcomes[job.id] = (True, json.loads(result_path.read_text(encoding="utf-8")), "")
        elif error_path.exists():
            outcomes[job.id] = (False, {}, f"Лог: {log_path}\n" + error_path.read_text(encoding="utf-8")[-3000:])
        else:   # процесс упал, не дойдя до задачи (нехватка памяти и т.п.)
            outcomes[job.id] = (False, {}, f"код {code}. Лог: {log_path}\n" + "\n".join(tail))
    return outcomes


def run_worker(cfg: dict, stop_when_empty: bool = True, poll_s: float = 3.0) -> dict[str, int]:
    lock = WorkerLock(output_dir(cfg, "queue.lock"))
    if not lock.acquire():
        raise RuntimeError("Воркер уже запущен (output/queue.lock занят) — задачи строго по одной")
    queue = open_queue(cfg)
    try:
        if stale := queue.reset_stale():
            log.info("Возвращено в очередь после сбоя: %d", stale)
        while True:
            job = queue.claim_next()
            if job is None:
                if stop_when_empty:
                    break
                time.sleep(poll_s)
                continue
            jobs = [job]
            if job.kind in BATCH_KINDS:
                jobs += queue.claim_ready(job.kind, cfg["queue"]["tts_batch_size"] - 1)
            t0 = time.perf_counter()
            extra = f" (+{len(jobs) - 1} в пачке — модель грузится один раз)" if len(jobs) > 1 else ""
            log.info("▶ #%d %s %s%s", job.id, job.kind, _short(job.payload), extra)
            outcomes = _run_subprocess(jobs, cfg)
            elapsed = time.perf_counter() - t0
            for j in jobs:
                ok, result, error = outcomes[j.id]
                if ok:
                    queue.complete(j.id, result)
                    for chain in result.get("next", []):
                        queue.enqueue_chain(chain, depends_on=j.id)
                    log.info("✓ #%d %s", j.id, j.kind)
                else:
                    status = queue.fail(j.id, error)
                    log.error("✗ #%d %s — %s\n%s", j.id, j.kind,
                              "будет повтор" if status == "pending" else "задача провалена", error)
            log.info("  %s — %.1f с", "пачка" if len(jobs) > 1 else f"#{job.id}", elapsed)
        return queue.counts()
    finally:
        queue.close()
        lock.release()


def run_jobs(job_ids: list[int], cfg: dict) -> int:
    """Выполняется в дочернем процессе: задачи по очереди → result.json / error.txt у каждой.
    Упавшая задача пачки не мешает остальным."""
    from pipeline.jobs import HANDLERS, JobContext
    queue = open_queue(cfg)
    jobs = [queue.get(i) for i in job_ids]
    queue.close()
    output_dir(cfg, "logs").mkdir(parents=True, exist_ok=True)
    ctx = JobContext(cfg)
    failed = 0
    try:
        for job in jobs:
            if len(jobs) > 1:
                print(f"── #{job.id} {job.kind} {_short(job.payload)}", flush=True)
            try:
                result = HANDLERS[job.kind](job.payload, cfg, ctx)
            except Exception:
                failed += 1
                trace = traceback.format_exc()
                print(trace, flush=True)
                _error_file(cfg, job).write_text(trace, encoding="utf-8")
                continue
            job_files(cfg, job)[1].write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    finally:
        ctx.close()
    return 1 if failed else 0


def _short(payload: dict) -> str:
    return " ".join(f"{k}={str(v).rsplit('/', 1)[-1]}" for k, v in payload.items())
