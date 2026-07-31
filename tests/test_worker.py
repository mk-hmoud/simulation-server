from __future__ import annotations

from pathlib import Path

import pytest

from simserver import db, queue as q
from simserver.backend import FakeBackend
from simserver.backend.base import BackendError, ErrorClass
from simserver.worker import Worker, _resolve_point_value, _slice_for_point


def make_worker(tmp_path: Path, **backend_kwargs) -> tuple[Worker, Path]:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"not a real model, just needs to exist")
    manifest = {
        "model_id": "spr_pcf_v1",
        "parameters": {
            "lambda0": {"unit": "nm", "min": 500, "max": 2000, "geometry": False},
            "pitch": {"unit": "um", "min": 1.0, "max": 5.0, "geometry": True},
        },
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
    q.register_model(conn, "m1", str(model_path), {"model_id": "m1", "parameters": {}})
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


class ArrayBackend:
    """Stub SolverBackend whose evaluate() returns per-mode arrays, to test
    mode_selection wiring end to end through Worker.process_one() — unlike
    FakeBackend, which only ever returns single scalars."""

    def __init__(self, neff_values: list[float], target: float) -> None:
        self.neff_values = neff_values
        self.target = target
        self.released: list[object] = []

    def load(self, model_path):
        return object()

    def set_parameters(self, handle, params):
        pass

    def build_geometry(self, handle):
        pass

    def mesh(self, handle):
        pass

    def solve(self, handle, study):
        pass

    def configure_sweep(self, handle, param_name, values, study=None):
        raise AssertionError("ArrayBackend doesn't support sweeps; use FakeBackend for that")

    def disable_sweep(self, handle):
        pass

    def evaluate(self, handle, expression: str):
        if expression == "real(emw.neff)":
            return list(self.neff_values)
        if expression == "n_silica":
            return self.target
        raise AssertionError(f"unexpected expression: {expression!r}")

    def export(self, handle, node, path):
        pass

    def release(self, handle):
        self.released.append(handle)


def test_process_one_with_mode_selection_writes_selected_mode_result_and_row(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"stub")
    manifest = {
        "model_id": "m1",
        "parameters": {},
        "outputs": {"neff_real": "real(emw.neff)"},
        "mode_selection": {
            "strategy": "nearest_neff_to_target",
            "neff_output": "neff_real",
            "neff_target_expression": "n_silica",
        },
    }
    q.register_model(conn, "m1", str(model_path), manifest)
    backend = ArrayBackend(neff_values=[1.30, 1.44, 1.50], target=1.45)
    worker = Worker(conn, backend, worker_id="worker-1")
    job_id = q.enqueue_job(conn, "m1", {}, outputs={"neff_real": "real(emw.neff)"})

    worker.process_one()

    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "done"

    result = conn.execute(
        "SELECT value_real FROM results WHERE job_id=? AND output_name='neff_real'", (job_id,)
    ).fetchone()
    assert result["value_real"] == 1.44  # index 1, nearest to target 1.45

    mode_rows = q.list_mode_selection(conn, job_id)
    assert len(mode_rows) == 1
    assert mode_rows[0]["strategy"] == "nearest_neff_to_target"
    assert mode_rows[0]["n_modes_considered"] == 3


def test_process_one_multivalued_output_without_mode_selection_is_validation_error(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"stub")
    q.register_model(conn, "m1", str(model_path), {"model_id": "m1", "parameters": {}})
    backend = ArrayBackend(neff_values=[1.30, 1.44, 1.50], target=1.45)
    worker = Worker(conn, backend, worker_id="worker-1")
    job_id = q.enqueue_job(conn, "m1", {}, outputs={"neff_real": "real(emw.neff)"})

    worker.process_one()

    row = conn.execute("SELECT status, error_class FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "failed"
    assert row["error_class"] == "validation"


def test_slice_for_point_degenerates_to_whole_list_when_one_point() -> None:
    assert _slice_for_point([1.1, 2.2, 3.3], 0, 1) == [1.1, 2.2, 3.3]


def test_slice_for_point_splits_evenly_across_points() -> None:
    flat = [1, 2, 3, 4, 5, 6]  # 2 points x 3 modes
    assert _slice_for_point(flat, 0, 2) == [1, 2, 3]
    assert _slice_for_point(flat, 1, 2) == [4, 5, 6]


def test_slice_for_point_raises_on_uneven_division() -> None:
    with pytest.raises(BackendError) as exc_info:
        _slice_for_point([1, 2, 3, 4, 5], 0, 2)
    assert exc_info.value.error_class == ErrorClass.INFRASTRUCTURE


def test_resolve_point_value_passes_through_single_value_ignoring_index() -> None:
    assert _resolve_point_value([42.0], selected_index=None) == 42.0
    assert _resolve_point_value([42.0], selected_index=3) == 42.0  # index irrelevant for non-mode-dependent


def test_resolve_point_value_requires_selected_index_for_multivalued() -> None:
    with pytest.raises(BackendError) as exc_info:
        _resolve_point_value([1.0, 2.0], selected_index=None)
    assert exc_info.value.error_class == ErrorClass.VALIDATION
    assert _resolve_point_value([1.0, 2.0], selected_index=1) == 2.0


def test_process_one_sweep_writes_one_result_row_per_point(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"stub")
    manifest = {
        "model_id": "m1",
        "parameters": {"lambda0": {"unit": "nm", "min": 500, "max": 2000, "geometry": False}},
        "sweep_parameter": "lambda0",
    }
    q.register_model(conn, "m1", str(model_path), manifest)
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    worker = Worker(conn, backend, worker_id="worker-1")
    job_id = q.enqueue_job(
        conn,
        "m1",
        {"lambda0": ["1550[nm]", "1560[nm]", "1570[nm]"]},
        outputs={"neff": "real(ewfd.neff)"},
    )

    worker.process_one()

    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "done"

    results = conn.execute(
        "SELECT sweep_index, sweep_value, value_real FROM results WHERE job_id=? ORDER BY sweep_index",
        (job_id,),
    ).fetchall()
    assert [r["sweep_index"] for r in results] == [0, 1, 2]
    assert [r["sweep_value"] for r in results] == [1550.0, 1560.0, 1570.0]
    assert len({r["value_real"] for r in results}) == 3  # each point's parameter differs, so values differ


def test_process_one_disables_sweep_for_a_subsequent_non_swept_job(tmp_path: Path) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    model_path = tmp_path / "model.mph"
    model_path.write_bytes(b"stub")
    manifest = {
        "model_id": "m1",
        "parameters": {"lambda0": {"unit": "nm", "min": 500, "max": 2000, "geometry": False}},
        "sweep_parameter": "lambda0",
    }
    q.register_model(conn, "m1", str(model_path), manifest)
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    worker = Worker(conn, backend, worker_id="worker-1")

    swept_job_id = q.enqueue_job(conn, "m1", {"lambda0": ["1550[nm]", "1560[nm]"]}, outputs={"neff": "real(ewfd.neff)"})
    plain_job_id = q.enqueue_job(conn, "m1", {"lambda0": "1550[nm]"}, outputs={"neff": "real(ewfd.neff)"})

    worker.process_one()
    worker.process_one()

    swept_results = conn.execute("SELECT * FROM results WHERE job_id=?", (swept_job_id,)).fetchall()
    plain_results = conn.execute("SELECT sweep_index, sweep_value FROM results WHERE job_id=?", (plain_job_id,)).fetchall()
    assert len(swept_results) == 2
    assert len(plain_results) == 1
    assert plain_results[0]["sweep_index"] == 0
    assert plain_results[0]["sweep_value"] is None


class SweepModeBackend:
    """Stub backend simulating a sweep with mode-dependent per-point neff
    arrays, to test the worker's per-sweep-point mode-selection slicing
    without the VM. target_repeated_per_mode toggles between the two
    plausible shapes a non-mode-dependent target expression might come back
    as under a real sweep (unverified against a real model either way) —
    _slice_for_point must handle both identically."""

    def __init__(
        self,
        neff_by_point: list[list[float]],
        target_by_point: list[float],
        *,
        target_repeated_per_mode: bool = False,
    ) -> None:
        self.neff_by_point = neff_by_point
        self.target_by_point = target_by_point
        self.target_repeated_per_mode = target_repeated_per_mode

    def load(self, model_path):
        return object()

    def set_parameters(self, handle, params):
        pass

    def build_geometry(self, handle):
        pass

    def mesh(self, handle):
        pass

    def solve(self, handle, study):
        pass

    def configure_sweep(self, handle, param_name, values, study=None):
        pass

    def disable_sweep(self, handle):
        pass

    def evaluate(self, handle, expression: str):
        if expression == "real(emw.neff)":
            flat: list[float] = []
            for point in self.neff_by_point:
                flat.extend(point)
            return flat
        if expression == "n_silica":
            if not self.target_repeated_per_mode:
                return list(self.target_by_point)
            flat = []
            for i, point in enumerate(self.neff_by_point):
                flat.extend([self.target_by_point[i]] * len(point))
            return flat
        raise AssertionError(f"unexpected expression: {expression!r}")

    def export(self, handle, node, path):
        pass

    def release(self, handle):
        pass


def _register_sweep_mode_model(conn) -> None:
    manifest = {
        "model_id": "m1",
        "parameters": {"l": {"unit": "um", "min": 0.5, "max": 2.0, "geometry": False}},
        "sweep_parameter": "l",
        "outputs": {"neff_real": "real(emw.neff)"},
        "mode_selection": {
            "strategy": "nearest_neff_to_target",
            "neff_output": "neff_real",
            "neff_target_expression": "n_silica",
        },
    }
    q.register_model(conn, "m1", "unused-path", manifest)


def _run_sweep_mode_selection_case(tmp_path: Path, *, target_repeated_per_mode: bool) -> None:
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    _register_sweep_mode_model(conn)
    backend = SweepModeBackend(
        neff_by_point=[[1.30, 1.44, 1.50], [1.20, 1.46, 1.55]],
        target_by_point=[1.45, 1.47],
        target_repeated_per_mode=target_repeated_per_mode,
    )
    worker = Worker(conn, backend, worker_id="worker-1")
    job_id = q.enqueue_job(conn, "m1", {"l": ["0.6[um]", "0.8[um]"]}, outputs={"neff_real": "real(emw.neff)"})

    worker.process_one()

    row = conn.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "done"

    results = conn.execute(
        "SELECT sweep_index, sweep_value, value_real FROM results WHERE job_id=? ORDER BY sweep_index",
        (job_id,),
    ).fetchall()
    assert [r["sweep_index"] for r in results] == [0, 1]
    assert [r["sweep_value"] for r in results] == [0.6, 0.8]
    assert results[0]["value_real"] == 1.44  # nearest to 1.45 among [1.30, 1.44, 1.50]
    assert results[1]["value_real"] == 1.46  # nearest to 1.47 among [1.20, 1.46, 1.55]

    mode_rows = q.list_mode_selection(conn, job_id)
    assert [r["sweep_index"] for r in mode_rows] == [0, 1]
    assert [r["n_modes_considered"] for r in mode_rows] == [3, 3]


def test_process_one_sweep_with_mode_selection_target_shape_per_point(tmp_path: Path) -> None:
    _run_sweep_mode_selection_case(tmp_path, target_repeated_per_mode=False)


def test_process_one_sweep_with_mode_selection_target_shape_repeated_per_mode(tmp_path: Path) -> None:
    _run_sweep_mode_selection_case(tmp_path, target_repeated_per_mode=True)
