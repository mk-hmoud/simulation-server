from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from simserver import db, queue as q
from simserver.supervisor import Supervisor, SupervisorConfig, WorkerStartupError

MANIFEST = {
    "model_id": "m1",
    "parameters": {},
    "outputs": {},
    "timeout_seconds": 0.2,
}


def make_supervisor(tmp_path: Path, **overrides) -> Supervisor:
    overrides.setdefault("worker_count", 0)
    config = SupervisorConfig(db_path=tmp_path / "jobs.db", **overrides)
    return Supervisor(config)


@pytest.fixture
def registered_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    q.register_model(conn, "m1", "models/m1/model.mph", MANIFEST)
    return conn


def spawn_sleeper(tmp_path: Path, spawn_child: bool = False) -> subprocess.Popen:
    script = tmp_path / "sleeper.py"
    if spawn_child:
        script.write_text(
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(9999)'])\n"
            "time.sleep(9999)\n"
        )
    else:
        script.write_text("import time\ntime.sleep(9999)\n")
    return subprocess.Popen([sys.executable, str(script)])


def test_kill_process_tree_terminates_parent_and_children(tmp_path: Path) -> None:
    supervisor = make_supervisor(tmp_path)
    proc = spawn_sleeper(tmp_path, spawn_child=True)
    time.sleep(0.5)  # let the child actually spawn
    parent = psutil.Process(proc.pid)
    child_pids = [c.pid for c in parent.children(recursive=True)]
    assert child_pids, "test setup: child process didn't spawn in time"

    supervisor._kill_process_tree(proc)

    assert proc.poll() is not None
    for pid in child_pids:
        assert not psutil.pid_exists(pid)


def test_start_raises_when_worker_dies_during_startup_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=1, startup_grace_s=2.0)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "raise SystemExit(1)"],
    )

    with pytest.raises(WorkerStartupError):
        supervisor.start()


def test_start_succeeds_when_workers_survive_grace_period(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=2, startup_grace_s=1.0)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    try:
        supervisor.start()
        assert set(supervisor._procs) == {"worker-1", "worker-2"}
        for proc in supervisor._procs.values():
            assert proc.poll() is None
    finally:
        supervisor.shutdown()


def test_poll_workers_reconciles_orphaned_job_and_respawns(
    tmp_path: Path, registered_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=1, max_attempts=2, retry_backoff_s=0)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    job_id = q.enqueue_job(registered_conn, "m1", {})
    q.claim_next_job(registered_conn, "worker-1")

    proc = spawn_sleeper(tmp_path)
    supervisor._procs["worker-1"] = proc
    proc.kill()
    proc.wait(timeout=5)

    try:
        supervisor._poll_workers()
        supervisor._top_up_pool()  # respawning now happens here, not inside _poll_workers()

        row = registered_conn.execute("SELECT status, attempt FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "queued"
        assert row["attempt"] == 2

        assert "worker-1" in supervisor._procs
        assert supervisor._procs["worker-1"].poll() is None
    finally:
        supervisor.shutdown()


def test_poll_workers_permanently_fails_job_after_max_attempts(
    tmp_path: Path, registered_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = make_supervisor(tmp_path, max_attempts=1, retry_backoff_s=0)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    job_id = q.enqueue_job(registered_conn, "m1", {})
    q.claim_next_job(registered_conn, "worker-1")

    proc = spawn_sleeper(tmp_path)
    supervisor._procs["worker-1"] = proc
    proc.kill()
    proc.wait(timeout=5)

    try:
        supervisor._poll_workers()
        row = registered_conn.execute("SELECT status, error_class FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "failed"
        assert row["error_class"] == "infrastructure"
    finally:
        supervisor.shutdown()


def test_watchdog_kills_hung_worker_and_reconciles_job(
    tmp_path: Path, registered_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    # MANIFEST declares timeout_seconds=0.2, so a job "started" now is already
    # overdue by the time we sleep past it
    supervisor = make_supervisor(tmp_path, worker_count=1, max_attempts=2, retry_backoff_s=0)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    job_id = q.enqueue_job(registered_conn, "m1", {})
    q.claim_next_job(registered_conn, "worker-1")

    proc = spawn_sleeper(tmp_path)
    supervisor._procs["worker-1"] = proc
    time.sleep(0.5)  # exceed the model's 0.2s timeout

    try:
        supervisor._check_watchdog()
        supervisor._top_up_pool()  # respawning now happens here, not inside _check_watchdog()

        assert proc.poll() is not None, "watchdog should have killed the hung worker process"
        row = registered_conn.execute("SELECT status, error_message FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "queued"
        assert "watchdog timeout" in row["error_message"]
        assert supervisor._procs["worker-1"].poll() is None
    finally:
        supervisor.shutdown()


def test_watchdog_leaves_jobs_within_timeout_alone(
    tmp_path: Path, registered_conn, monkeypatch: pytest.MonkeyPatch
) -> None:
    conn = registered_conn
    q.register_model(conn, "m2", "models/m2/model.mph", {**MANIFEST, "model_id": "m2", "timeout_seconds": 9999})
    supervisor = make_supervisor(tmp_path)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    job_id = q.enqueue_job(conn, "m2", {})
    q.claim_next_job(conn, "worker-1")
    proc = spawn_sleeper(tmp_path)
    supervisor._procs["worker-1"] = proc

    try:
        supervisor._check_watchdog()
        assert proc.poll() is None
        row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
        assert row["status"] == "running"
    finally:
        supervisor.shutdown()


def test_top_up_pool_spawns_missing_workers_up_to_configured_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=2)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    try:
        supervisor._top_up_pool()
        assert set(supervisor._procs) == {"worker-1", "worker-2"}
    finally:
        supervisor.shutdown()


def test_top_up_pool_does_not_spawn_during_maintenance_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=2)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )
    q.set_maintenance_mode(supervisor.conn, True)

    try:
        supervisor._top_up_pool()
        assert supervisor._procs == {}
    finally:
        supervisor.shutdown()


def test_top_up_pool_leaves_existing_workers_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    supervisor = make_supervisor(tmp_path, worker_count=1)
    monkeypatch.setattr(
        supervisor,
        "_worker_cmd",
        lambda worker_id: [sys.executable, "-c", "import time; time.sleep(9999)"],
    )

    try:
        supervisor._top_up_pool()
        first_proc = supervisor._procs["worker-1"]
        supervisor._top_up_pool()  # already at target size — must not touch it
        assert supervisor._procs["worker-1"] is first_proc
    finally:
        supervisor.shutdown()
