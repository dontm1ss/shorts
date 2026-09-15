"""Очередь задач на SQLite (ТЗ, раздел 6): без внешних зависимостей, строго последовательно.

Задача = (kind, payload). Задача может зависеть от другой (depends_on) — берётся в работу,
только когда зависимость выполнена. Упавшая задача повторяется до max_attempts раз, после
чего помечается failed, а все зависящие от неё — cancelled.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Collection
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT    NOT NULL,
    payload      TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',   -- pending | running | done | failed | cancelled
    depends_on   INTEGER REFERENCES jobs(id),
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    result       TEXT,
    error        TEXT,
    created_at   TEXT    NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Job:
    id: int
    kind: str
    payload: dict
    status: str
    depends_on: int | None
    attempts: int
    max_attempts: int
    result: dict | None
    error: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        d = dict(row)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return cls(**d)


class JobQueue:
    def __init__(self, db_path: Path, max_attempts: int = 2):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_attempts = max_attempts
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def _tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    # --- постановка -----------------------------------------------------------------

    def enqueue(self, kind: str, payload: dict, depends_on: int | None = None,
                max_attempts: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO jobs (kind, payload, depends_on, max_attempts, created_at) VALUES (?, ?, ?, ?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False), depends_on,
             max_attempts or self.max_attempts, _now()))
        return cur.lastrowid

    def enqueue_chain(self, jobs: list[dict], depends_on: int | None = None) -> list[int]:
        """[{kind, payload}, ...] — каждая следующая зависит от предыдущей."""
        ids = []
        for job in jobs:
            depends_on = self.enqueue(job["kind"], job["payload"], depends_on)
            ids.append(depends_on)
        return ids

    # --- выполнение -----------------------------------------------------------------

    def claim_next(self, kinds: Collection[str] | None = None, exclude: Collection[str] = (),
                   later: Collection[str] = ()) -> Job | None:
        """Следующая готовая задача по порядку постановки. kinds — только эти виды,
        exclude — кроме этих, later — эти виды берутся, только когда других готовых нет."""
        where, order, args = [], "j.id", []
        if kinds is not None:
            if not kinds:
                return None
            where.append(f"j.kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        if exclude:
            where.append(f"j.kind NOT IN ({', '.join('?' * len(exclude))})")
            args += exclude
        if later:
            order = f"j.kind IN ({', '.join('?' * len(later))}), j.id"
        with self._tx() as c:
            row = c.execute(f"""
                SELECT j.* FROM jobs j LEFT JOIN jobs d ON d.id = j.depends_on
                WHERE j.status = 'pending' AND (j.depends_on IS NULL OR d.status = 'done')
                {''.join(' AND ' + w for w in where)}
                ORDER BY {order} LIMIT 1""", (*args, *later)).fetchone()
            if row is None:
                return None
            c.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, error=NULL "
                      "WHERE id=?", (_now(), row["id"]))
        return self.get(row["id"])

    def claim_ready(self, kind: str, limit: int) -> list[Job]:
        """Ещё до limit готовых задач того же вида — выполнить пачкой в одном процессе
        (модель TTS грузится один раз на пачку, а не на каждый ролик)."""
        if limit <= 0:
            return []
        with self._tx() as c:
            ids = [r["id"] for r in c.execute("""
                SELECT j.id FROM jobs j LEFT JOIN jobs d ON d.id = j.depends_on
                WHERE j.status = 'pending' AND j.kind = ? AND (j.depends_on IS NULL OR d.status = 'done')
                ORDER BY j.id LIMIT ?""", (kind, limit))]
            for job_id in ids:
                c.execute("UPDATE jobs SET status='running', attempts=attempts+1, started_at=?, error=NULL "
                          "WHERE id=?", (_now(), job_id))
        return [self.get(i) for i in ids]

    def complete(self, job_id: int, result: dict | None = None) -> None:
        self.conn.execute("UPDATE jobs SET status='done', result=?, finished_at=? WHERE id=?",
                          (json.dumps(result or {}, ensure_ascii=False), _now(), job_id))

    def fail(self, job_id: int, error: str) -> str:
        """Возвращает новый статус: pending (будет повтор) или failed."""
        job = self.get(job_id)
        status = "pending" if job.attempts < job.max_attempts else "failed"
        with self._tx() as c:
            c.execute("UPDATE jobs SET status=?, error=?, finished_at=? WHERE id=?",
                      (status, error[-4000:], _now(), job_id))
            if status == "failed":
                self._cascade(c, job_id, "cancelled")
        return status

    def _cascade(self, c: sqlite3.Connection, job_id: int, status: str) -> None:
        children = [r["id"] for r in c.execute(
            "SELECT id FROM jobs WHERE depends_on=? AND status IN ('pending','cancelled')", (job_id,))]
        for child in children:
            c.execute("UPDATE jobs SET status=? WHERE id=?", (status, child))
            self._cascade(c, child, status)

    def retry(self, job_id: int) -> None:
        """Вернуть упавшую задачу и отменённые зависимые в очередь."""
        with self._tx() as c:
            c.execute("UPDATE jobs SET status='pending', attempts=0, error=NULL WHERE id=?", (job_id,))
            self._cascade(c, job_id, "pending")

    def reset_stale(self) -> int:
        """Задачи в статусе running после падения воркера — обратно в pending."""
        return self.conn.execute("UPDATE jobs SET status='pending' WHERE status='running'").rowcount

    # --- чтение ---------------------------------------------------------------------

    def get(self, job_id: int) -> Job:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(f"Нет задачи #{job_id}")
        return Job.from_row(row)

    def jobs(self, status: str | None = None, limit: int = 200, kinds: Collection[str] | None = None) -> list[Job]:
        where, args = [], []
        if status:
            where.append("status=?")
            args.append(status)
        if kinds:
            where.append(f"kind IN ({', '.join('?' * len(kinds))})")
            args += kinds
        sql = "SELECT * FROM jobs" + (" WHERE " + " AND ".join(where) if where else "")
        rows = self.conn.execute(sql + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [Job.from_row(r) for r in reversed(rows)]

    def counts(self) -> dict[str, int]:
        return {r["status"]: r["n"] for r in
                self.conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


class WorkerLock:
    """Один воркер на очередь: блокировка файла снимается ОС при завершении процесса."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            return False
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return True

    def release(self) -> None:
        if self._fh:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                self._fh.close()
                self._fh = None
