from __future__ import annotations

import csv
import io
from pathlib import Path

from simserver import db, queue as q
from simserver.dataset import batch_dataset_csv, batch_dataset_rows


def make_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    return conn


def test_empty_batch_returns_no_rows(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    rows, fieldnames = batch_dataset_rows(conn, "does-not-exist")
    assert rows == []
    assert fieldnames == []


def test_batch_with_no_results_yet_returns_no_rows(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    q.enqueue_job(conn, "m1", {"p": "1.8[um]"}, batch_id="batch-1")

    rows, fieldnames = batch_dataset_rows(conn, "batch-1")
    assert rows == []


def test_flattens_scalar_jobs_with_params_and_outputs(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {"p": "1.8[um]"}, batch_id="batch-1")
    j2 = q.enqueue_job(conn, "m1", {"p": "2.0[um]"}, batch_id="batch-1")

    q.write_result(conn, j1, "neff_real", 1.44)
    q.write_result(conn, j2, "neff_real", 1.45)

    rows, fieldnames = batch_dataset_rows(conn, "batch-1")
    assert fieldnames == ["job_id", "sweep_index", "sweep_value", "p", "neff_real"]
    assert len(rows) == 2
    assert rows[0] == {"job_id": j1, "sweep_index": 0, "sweep_value": None, "p": "1.8[um]", "neff_real": 1.44}
    assert rows[1] == {"job_id": j2, "sweep_index": 0, "sweep_value": None, "p": "2.0[um]", "neff_real": 1.45}


def test_sweep_job_produces_one_row_per_sweep_index(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {"l": ["0.6[um]", "0.8[um]"]}, batch_id="batch-1")

    q.write_result(conn, j1, "neff_real", 1.40, sweep_index=0, sweep_value=0.6)
    q.write_result(conn, j1, "neff_real", 1.45, sweep_index=1, sweep_value=0.8)

    rows, fieldnames = batch_dataset_rows(conn, "batch-1")
    assert "l" not in fieldnames  # sweep param excluded from flat columns; sweep_value covers it
    assert len(rows) == 2
    assert rows[0]["sweep_value"] == 0.6
    assert rows[1]["sweep_value"] == 0.8


def test_complex_output_gets_an_imag_column_only_when_used(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    j2 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")

    q.write_result(conn, j1, "neff", complex(1.44, -0.01))
    q.write_result(conn, j2, "neff", 1.45)  # plain real value, no imag column needed here specifically

    rows, fieldnames = batch_dataset_rows(conn, "batch-1")
    assert "neff__imag" in fieldnames
    row_by_job = {r["job_id"]: r for r in rows}
    assert row_by_job[j1]["neff__imag"] == -0.01
    assert row_by_job[j2]["neff"] == 1.45


def test_excludes_jobs_from_other_batches(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {}, batch_id="batch-1")
    j2 = q.enqueue_job(conn, "m1", {}, batch_id="batch-2")
    q.write_result(conn, j1, "neff", 1.0)
    q.write_result(conn, j2, "neff", 2.0)

    rows, _ = batch_dataset_rows(conn, "batch-1")
    assert len(rows) == 1
    assert rows[0]["job_id"] == j1


def test_batch_dataset_csv_is_parseable(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", "models/m1/model.mph")
    j1 = q.enqueue_job(conn, "m1", {"p": "1.8[um]"}, batch_id="batch-1")
    q.write_result(conn, j1, "neff_real", 1.44)

    csv_text = batch_dataset_csv(conn, "batch-1")
    reader = csv.DictReader(io.StringIO(csv_text))
    rows = list(reader)
    assert len(rows) == 1
    assert rows[0]["p"] == "1.8[um]"
    assert rows[0]["neff_real"] == "1.44"


def test_batch_dataset_csv_empty_batch_is_empty_string(tmp_path: Path) -> None:
    conn = make_conn(tmp_path)
    assert batch_dataset_csv(conn, "does-not-exist") == ""
