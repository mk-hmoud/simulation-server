from __future__ import annotations

from pathlib import Path

from simserver import db, queue as q
from simserver.backend import FakeBackend
from simserver.worker import Worker


def make_worker(tmp_path: Path, **backend_kwargs) -> tuple[Worker, Path]:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"not a real model, just needs to exist")
    manifest = {
        "parameters": {
            "lambda0": {"geometry": False},
            "pitch": {"geometry": True},
        }
    }
    q.register_model(conn, "spr_pcf_v1", str(model_path), manifest)
    worker = Worker(conn, FakeBackend(solve_seconds=0, mesh_seconds=0), worker_id="worker-1")
    return worker, model_path


def test_process_one_runs_job_to_completion_and_writes_results(tmp_path: Path) -> None:
    worker, _ = make_worker(tmp_path)
    job_id = q.enqueue_job(
        worker.conn,
        "spr_pcf_v1",
        {"lambda0": "1550[nm]"},
        outputs={"neff": "real(ewfd.neff)"},
    )

    processed = worker.process_one()
    assert processed is True

    row = worker.conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "done"

    result = worker.conn.execute(
        "SELECT value_real FROM results WHERE job_id=? AND output_name=?", (job_id, "neff")
    ).fetchone()
    assert result is not None
    assert isinstance(result["value_real"], float)


def test_process_one_returns_false_when_queue_empty(tmp_path: Path) -> None:
    worker, _ = make_worker(tmp_path)
    assert worker.process_one() is False


def test_unknown_model_id_marks_job_failed_validation(tmp_path: Path) -> None:
    # the models(id) foreign key normally prevents this at enqueue time; this
    # test bypasses it once to exercise the worker's own defensive check, for
    # the case where a job outlives its model row (e.g. deleted after enqueue)
    worker, _ = make_worker(tmp_path)
    worker.conn.execute("PRAGMA foreign_keys=OFF")
    job_id = q.enqueue_job(worker.conn, "does-not-exist", {})
    worker.conn.execute("PRAGMA foreign_keys=ON")

    worker.process_one()

    row = worker.conn.execute(
        "SELECT status, error_class FROM jobs WHERE id=?", (job_id,)
    ).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "validation"


def test_simulated_non_convergence_marks_job_failed_solver(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"stub")
    q.register_model(conn, "m1", str(model_path))
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0, non_convergence_rate=1.0, seed=0)
    worker = Worker(conn, backend, worker_id="worker-1")
    job_id = q.enqueue_job(conn, "m1", {})

    worker.process_one()

    row = conn.execute("SELECT status, error_class FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "solver"


def test_geometry_parameter_change_triggers_rebuild_and_mesh(tmp_path: Path) -> None:
    worker, _ = make_worker(tmp_path)
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"pitch": "1.8[um]"})
    worker.process_one()
    handle_after_first = worker._handle
    assert handle_after_first.geometry_built is True
    assert handle_after_first.meshed is True

    # reset the flags to prove the second solve actually re-triggers them
    handle_after_first.geometry_built = False
    handle_after_first.meshed = False
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"pitch": "2.0[um]"})
    worker.process_one()

    assert worker._handle is handle_after_first
    assert worker._handle.geometry_built is True
    assert worker._handle.meshed is True


def test_non_geometry_parameter_change_skips_rebuild(tmp_path: Path) -> None:
    worker, _ = make_worker(tmp_path)
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"lambda0": "1550[nm]"})
    worker.process_one()

    handle = worker._handle
    handle.geometry_built = False
    handle.meshed = False
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"lambda0": "1600[nm]"})
    worker.process_one()

    # non-geometry-only change: build_geometry()/mesh() must not have been called again
    assert handle.geometry_built is False
    assert handle.meshed is False


def test_worker_reuses_loaded_model_across_jobs_of_the_same_model(tmp_path: Path) -> None:
    worker, _ = make_worker(tmp_path)
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"lambda0": "1550[nm]"})
    q.enqueue_job(worker.conn, "spr_pcf_v1", {"lambda0": "1600[nm]"})

    worker.process_one()
    first_handle = worker._handle
    worker.process_one()

    assert worker._handle is first_handle
