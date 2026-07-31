from __future__ import annotations

import threading
import time
from pathlib import Path

from simserver import db, queue as q


def make_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    return conn


def test_enqueue_and_claim_roundtrip(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    job_id = q.enqueue_job(conn, "m1", {"lambda0": "1550[nm]"}, outputs={"neff": "real(ewfd.neff)"})

    job = q.claim_next_job(conn, "worker-1")
    assert job is not None
    assert job.id == job_id
    assert job.model_id == "m1"
    assert job.params == {"lambda0": "1550[nm]"}
    assert job.outputs == {"neff": "real(ewfd.neff)"}

    status = conn.execute("SELECT status, worker_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert status["status"] == "running"
    assert status["worker_id"] == "worker-1"


def test_claim_returns_none_when_queue_empty(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert q.claim_next_job(conn, "worker-1") is None


def test_claim_prefers_priority_then_age(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    low = q.enqueue_job(conn, "m1", {}, priority=0)
    high = q.enqueue_job(conn, "m1", {}, priority=5)

    job = q.claim_next_job(conn, "worker-1")
    assert job.id == high

    job = q.claim_next_job(conn, "worker-1")
    assert job.id == low


def test_claim_prefers_matching_model_affinity_over_fifo_order(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    q.register_model(conn, "m2", "models/m2/model.mph")
    first_queued = q.enqueue_job(conn, "m1", {})  # oldest overall, but not the affinity model
    affinity_match = q.enqueue_job(conn, "m2", {})

    job = q.claim_next_job(conn, "worker-1", preferred_model_id="m2")
    assert job.id == affinity_match

    job = q.claim_next_job(conn, "worker-1", preferred_model_id="m2")
    assert job.id == first_queued


def test_mark_done_and_mark_failed(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    done_id = q.enqueue_job(conn, "m1", {})
    failed_id = q.enqueue_job(conn, "m1", {})

    q.claim_next_job(conn, "worker-1")
    q.claim_next_job(conn, "worker-1")
    q.mark_done(conn, done_id)
    q.mark_failed(conn, failed_id, "solver", "non-convergence")

    row = conn.execute("SELECT status FROM jobs WHERE id=?", (done_id,)).fetchone()
    assert row["status"] == "done"

    row = conn.execute("SELECT status, error_class, error_message FROM jobs WHERE id=?", (failed_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "solver"
    assert row["error_message"] == "non-convergence"


def test_write_result_splits_complex_value(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    job_id = q.enqueue_job(conn, "m1", {})

    q.write_result(conn, job_id, "neff", complex(1.44, -0.001))
    row = conn.execute("SELECT value_real, value_imag FROM results WHERE job_id=?", (job_id,)).fetchone()
    assert row["value_real"] == 1.44
    assert row["value_imag"] == -0.001


def test_concurrent_claims_never_double_claim_the_same_job(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"
    setup_conn = db.connect(db_path)
    db.init_db(setup_conn)
    q.register_model(setup_conn, "m1", "models/m1/model.mph")
    job_ids = [q.enqueue_job(setup_conn, "m1", {}) for _ in range(20)]

    claimed: list[int] = []
    lock = threading.Lock()

    def worker_loop(worker_id: str) -> None:
        conn = db.connect(db_path)
        while True:
            job = q.claim_next_job(conn, worker_id)
            if job is None:
                return
            with lock:
                claimed.append(job.id)

    threads = [threading.Thread(target=worker_loop, args=(f"worker-{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert sorted(claimed) == sorted(job_ids)


def test_requeue_or_fail_requeues_with_backoff_when_attempts_remain(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    job_id = q.enqueue_job(conn, "m1", {})
    q.claim_next_job(conn, "worker-1")

    result = q.requeue_or_fail(conn, job_id, "infrastructure", "worker crashed", max_attempts=2, backoff_seconds=60)
    assert result == "queued"

    row = conn.execute(
        "SELECT status, attempt, worker_id, started_at, not_before, error_class FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    assert row["status"] == "queued"
    assert row["attempt"] == 2
    assert row["worker_id"] is None
    assert row["started_at"] is None
    assert row["error_class"] == "infrastructure"
    assert row["not_before"] > time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    # not claimable yet: not_before is in the future
    assert q.claim_next_job(conn, "worker-2") is None


def test_requeue_or_fail_permanently_fails_once_attempts_exhausted(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    job_id = q.enqueue_job(conn, "m1", {})
    q.claim_next_job(conn, "worker-1")

    q.requeue_or_fail(conn, job_id, "infrastructure", "first crash", max_attempts=2, backoff_seconds=0)
    # claimable again immediately (backoff_seconds=0)
    job = q.claim_next_job(conn, "worker-1")
    assert job is not None

    result = q.requeue_or_fail(conn, job_id, "infrastructure", "second crash", max_attempts=2, backoff_seconds=0)
    assert result == "failed"

    row = conn.execute("SELECT status, error_message FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_message"] == "second crash"


def test_claim_ignores_jobs_whose_not_before_has_passed(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    job_id = q.enqueue_job(conn, "m1", {})
    q.claim_next_job(conn, "worker-1")
    q.requeue_or_fail(conn, job_id, "infrastructure", "crash", max_attempts=5, backoff_seconds=0)

    job = q.claim_next_job(conn, "worker-2")
    assert job is not None
    assert job.id == job_id


def test_maintenance_mode_defaults_off(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert q.get_maintenance_mode(conn) is False


def test_maintenance_mode_toggles(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.set_maintenance_mode(conn, True)
    assert q.get_maintenance_mode(conn) is True
    q.set_maintenance_mode(conn, False)
    assert q.get_maintenance_mode(conn) is False


def test_claim_next_job_refuses_to_claim_during_maintenance(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    q.enqueue_job(conn, "m1", {})
    q.set_maintenance_mode(conn, True)

    assert q.claim_next_job(conn, "worker-1") is None

    q.set_maintenance_mode(conn, False)
    job = q.claim_next_job(conn, "worker-1")
    assert job is not None


def test_batch_summary_none_for_unknown_batch(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert q.batch_summary(conn, "does-not-exist") is None


def test_batch_summary_counts_by_status(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    q.enqueue_job(conn, "m1", {})  # not in the batch

    q.claim_next_job(conn, "worker-1")  # claims j1 (oldest queued)
    q.mark_done(conn, j1)

    summary = q.batch_summary(conn, "batch-1")
    assert summary["total"] == 2
    assert summary["depth"] == {"done": 1, "queued": 1}


def test_list_batch_jobs_excludes_other_batches(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    j2 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    q.enqueue_job(conn, "m1", {}, batch_id="batch-2")

    jobs = q.list_batch_jobs(conn, "batch-1")
    assert [j["id"] for j in jobs] == [j1, j2]
