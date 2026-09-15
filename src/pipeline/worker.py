"""Воркер очереди: берёт задачи и выполняет каждую в отдельном процессе.

Две линии. Тяжёлая — озвучка, whisper, запись интерфейса, рендер — строго по одной задаче.
Лёгкая — сценарии DeepSeek: это ожидание ответа по сети, машину они почти не грузят, поэтому
идут параллельно тяжёлой (queue.light_lane) — одобренные ролики не ждут генерацию новых
сценариев, а ожидание DeepSeek не простаивает видеокарту. Без лёгкой линии сценарии
берутся после всех готовых задач обработки.

Озвучка может идти пачкой (queue.tts_batch_size): готовые задачи tts_synthesis одним процессом
друг за другом, модели грузятся один раз.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import traceback
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path

from core.config import ROOT
from core.scene import output_dir
from notify import Digest
from pipeline.queue import Job, JobQueue, WorkerLock

log = logging.getLogger(__name__)

BATCH_KINDS = {"tts_synthesis"}
LIGHT_KINDS = ("script_story", "script_drama", "script_batch")   # сеть (DeepSeek, форумы), не GPU

# Windows: задача получает скрытую консоль. Иначе у воркера без консоли (запуск из панели)
# каждая задача открывала своё окно — закроешь его, и озвучка падает
# («forrtl: error (200): program aborting due to window-CLOSE event»).
NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


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
                                text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
        for line in proc.stdout:
            fh.write(line)
            line = line.rstrip()
            if line and "\r" not in line and not line.startswith(("Sampling", "Fetching")):
                print(f"    │ #{jobs[0].id} {line}", flush=True)   # линии идут параллельно — строки вперемешку
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


def _claim(queue: JobQueue, lane: str, cfg: dict) -> list[Job]:
    """Задачи для линии: light — сценарии; heavy — обработка (озвучка — пачкой до tts_batch_size)."""
    if lane == "light":
        job = queue.claim_next(kinds=LIGHT_KINDS)
    elif cfg["queue"].get("light_lane", True):
        job = queue.claim_next(exclude=LIGHT_KINDS)
    else:   # одна линия: сценарии — только когда обработке нечего делать
        job = queue.claim_next(later=LIGHT_KINDS)
    if job is None:
        return []
    jobs = [job]
    if job.kind in BATCH_KINDS:
        jobs += queue.claim_ready(job.kind, cfg["queue"]["tts_batch_size"] - 1)
    return jobs


def _finish(queue: JobQueue, jobs: list[Job], future: Future, elapsed: float, digest: Digest) -> None:
    try:
        outcomes = future.result()
    except Exception as e:   # процесс задачи не удалось даже запустить
        outcomes = {j.id: (False, {}, f"{type(e).__name__}: {e}") for j in jobs}
    for j in jobs:
        ok, result, error = outcomes[j.id]
        if ok:
            queue.complete(j.id, result)
            for chain in result.get("next", []):
                queue.enqueue_chain(chain, depends_on=j.id)
            log.info("✓ #%d %s", j.id, j.kind)
            digest.job_done(j, result)
        else:
            status = queue.fail(j.id, error)
            log.error("✗ #%d %s — %s\n%s", j.id, j.kind,
                      "будет повтор" if status == "pending" else "задача провалена", error)
            if status == "failed":
                digest.job_failed(j, error)
    log.info("  %s — %.1f с", "пачка" if len(jobs) > 1 else f"#{jobs[0].id}", elapsed)


def run_worker(cfg: dict, stop_when_empty: bool = True, poll_s: float = 3.0) -> dict[str, int]:
    lock = WorkerLock(output_dir(cfg, "queue.lock"))
    if not lock.acquire():
        raise RuntimeError("Воркер уже запущен (output/queue.lock занят) — задачи строго по одной")
    queue = open_queue(cfg)   # очередь — только в этом потоке; в потоках линий — лишь ожидание процессов
    lanes = ("heavy", "light") if cfg["queue"].get("light_lane", True) else ("heavy",)
    running: dict[str, tuple[Future, list[Job], float]] = {}
    pool = ThreadPoolExecutor(max_workers=len(lanes), thread_name_prefix="lane")
    digest = Digest(cfg)   # сводки в Telegram: упавшие задачи, готовые ролики, сценарии на проверке
    try:
        if stale := queue.reset_stale():
            log.info("Возвращено в очередь после сбоя: %d", stale)
        while True:
            for lane in lanes:
                if lane in running or not (jobs := _claim(queue, lane, cfg)):
                    continue
                extra = f" (+{len(jobs) - 1} в пачке — модель грузится один раз)" if len(jobs) > 1 else ""
                log.info("▶ #%d %s %s%s", jobs[0].id, jobs[0].kind, _short(jobs[0].payload), extra)
                running[lane] = (pool.submit(_run_subprocess, jobs, cfg), jobs, time.perf_counter())
            digest.tick(idle=not running)
            if not running:
                if stop_when_empty:
                    break
                time.sleep(poll_s)
                continue
            # пока одна линия занята, другая раз в poll_s проверяет, не появились ли задачи для неё
            wait([future for future, _, _ in running.values()], timeout=poll_s, return_when=FIRST_COMPLETED)
            for lane, (future, jobs, t0) in list(running.items()):
                if future.done():
                    del running[lane]
                    _finish(queue, jobs, future, time.perf_counter() - t0, digest)
        return queue.counts()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        queue.close()
        lock.release()
        digest.flush()


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
