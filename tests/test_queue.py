import pytest

from pipeline.queue import JobQueue, WorkerLock


@pytest.fixture
def queue(tmp_path):
    q = JobQueue(tmp_path / "q.sqlite", max_attempts=2)
    yield q
    q.close()


def test_dependencies_run_in_order(queue):
    a, b, c = queue.enqueue_chain([{"kind": "k1", "payload": {"n": 1}},
                                   {"kind": "k2", "payload": {"n": 2}},
                                   {"kind": "k3", "payload": {"n": 3}}])
    free = queue.enqueue("other", {})
    assert queue.claim_next().id == a
    assert queue.claim_next().id == free        # b ждёт a, берётся независимая задача
    assert queue.claim_next() is None
    queue.complete(a, {"ok": True})
    job = queue.claim_next()
    assert job.id == b and job.payload == {"n": 2}
    queue.complete(b)
    assert queue.claim_next().id == c
    assert queue.get(a).result == {"ok": True}


def test_retry_then_fail_cascades(queue):
    a, b, c = queue.enqueue_chain([{"kind": "k", "payload": {}}] * 3)
    job = queue.claim_next()
    assert queue.fail(job.id, "boom") == "pending"          # первая попытка — будет повтор
    job = queue.claim_next()
    assert job.id == a and job.attempts == 2
    assert queue.fail(job.id, "boom again") == "failed"
    assert [queue.get(i).status for i in (a, b, c)] == ["failed", "cancelled", "cancelled"]
    assert queue.claim_next() is None

    queue.retry(a)                                           # ручной повтор оживляет цепочку
    assert [queue.get(i).status for i in (a, b, c)] == ["pending"] * 3
    assert queue.claim_next().id == a


def test_reset_stale(queue):
    queue.enqueue("k", {})
    queue.claim_next()
    assert queue.reset_stale() == 1
    assert queue.counts() == {"pending": 1}


def test_worker_lock_is_exclusive(tmp_path):
    first, second = WorkerLock(tmp_path / "w.lock"), WorkerLock(tmp_path / "w.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()
