"""Воркер очереди: берёт задачи по одной и выполняет каждую в отдельном процессе."""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from core.config import ROOT
from core.scene import output_dir
from pipeline.queue import Job, JobQueue, WorkerLock

log = logging.getLogger(__name__)


def open_queue(cfg: dict) -> JobQueue:
    return JobQueue(ROOT / cfg["queue"]["db"], cfg["queue"]["max_attempts"])


def job_files(cfg: dict, job: Job) -> tuple[Path, Path]:
    logs = output_dir(cfg, "logs")
    return logs / f"job_{job.id:05d}_{job.kind}.log", logs / f"job_{job.id:05d}.result.json"


def _run_subprocess(job: Job, cfg: dict) -> tuple[bool, dict, str]:
    log_path, result_path = job_files(cfg, job)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.unlink(missing_ok=True)
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
           "SHORTS_PROFILE": cfg["profile"]["name"]}
    cmd = [sys.executable, "-m", "pipeline", "job", str(job.id)]
    tail: list[str] = []
    with open(log_path, "a", encoding="utf-8") as fh:
        fh.write(f"\n=== попытка {job.attempts}, {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout:
            fh.write(line)
            line = line.rstrip()
            if line and "\r" not in line and not line.startswith(("Sampling", "Fetching")):
                print(f"    │ {line}", flush=True)
            tail = (tail + [line])[-20:]
        code = proc.wait()
    if code == 0 and result_path.exists():
        return True, json.loads(result_path.read_text(encoding="utf-8")), ""
    return False, {}, f"код {code}. Лог: {log_path}\n" + "\n".join(tail)


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
            t0 = time.perf_counter()
            log.info("▶ #%d %s %s", job.id, job.kind, _short(job.payload))
            ok, result, error = _run_subprocess(job, cfg)
            elapsed = time.perf_counter() - t0
            if ok:
                queue.complete(job.id, result)
                for chain in result.get("next", []):
                    queue.enqueue_chain(chain, depends_on=job.id)
                log.info("✓ #%d %s — %.1f с", job.id, job.kind, elapsed)
            else:
                status = queue.fail(job.id, error)
                log.error("✗ #%d %s — %.1f с, %s\n%s", job.id, job.kind, elapsed,
                          "будет повтор" if status == "pending" else "задача провалена", error)
        return queue.counts()
    finally:
        queue.close()
        lock.release()


def run_job(job_id: int, cfg: dict) -> int:
    """Выполняется в дочернем процессе: одна задача → result.json."""
    from pipeline.jobs import HANDLERS
    queue = open_queue(cfg)
    job = queue.get(job_id)
    queue.close()
    result = HANDLERS[job.kind](job.payload, cfg)
    _, result_path = job_files(cfg, job)
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


def _short(payload: dict) -> str:
    return " ".join(f"{k}={str(v).rsplit('/', 1)[-1]}" for k, v in payload.items())
