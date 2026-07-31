from __future__ import annotations

import json
from pathlib import Path

import pytest

from simserver import cli, db, queue as q

MANIFEST = {
    "model_id": "m1",
    "parameters": {},
    "outputs": {"neff_real": "real(emw.neff)", "neff_imag": "imag(emw.neff)"},
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    db.init_db(conn)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    return path


def run(db_path: Path, *args: str) -> None:
    cli.main(["--db", str(db_path), *args])


def test_enqueue_without_outputs_defaults_to_all_manifest_outputs(db_path: Path) -> None:
    run(db_path, "enqueue", "m1")

    conn = db.connect(db_path)
    row = conn.execute("SELECT outputs_json FROM jobs WHERE id=1").fetchone()
    assert json.loads(row["outputs_json"]) == MANIFEST["outputs"]


def test_enqueue_with_explicit_empty_outputs_stays_empty(db_path: Path) -> None:
    run(db_path, "enqueue", "m1", "--outputs", "{}")

    conn = db.connect(db_path)
    row = conn.execute("SELECT outputs_json FROM jobs WHERE id=1").fetchone()
    assert json.loads(row["outputs_json"]) == {}


def test_enqueue_with_explicit_outputs_subset(db_path: Path) -> None:
    run(db_path, "enqueue", "m1", "--outputs", '{"neff_real": "real(emw.neff)"}')

    conn = db.connect(db_path)
    row = conn.execute("SELECT outputs_json FROM jobs WHERE id=1").fetchone()
    assert json.loads(row["outputs_json"]) == {"neff_real": "real(emw.neff)"}


def test_enqueue_params_file_handles_utf8_bom(db_path: Path, tmp_path: Path) -> None:
    params_file = tmp_path / "params.json"
    params_file.write_bytes(b"\xef\xbb\xbf" + b'{"l": "0.8[um]"}')  # UTF-8 BOM prefix

    run(db_path, "enqueue", "m1", "--params-file", str(params_file))

    conn = db.connect(db_path)
    row = conn.execute("SELECT params_json FROM jobs WHERE id=1").fetchone()
    assert json.loads(row["params_json"]) == {"l": "0.8[um]"}


def test_enqueue_unknown_model_with_default_outputs_errors_clearly(db_path: Path) -> None:
    with pytest.raises(SystemExit, match="unknown model_id"):
        run(db_path, "enqueue", "does-not-exist")


def test_batch_enqueues_one_job_per_entry_with_shared_batch_id(db_path: Path) -> None:
    run(db_path, "batch", "m1", "--params-list", '[{"pitch": "1[um]"}, {"pitch": "2[um]"}]')

    conn = db.connect(db_path)
    jobs = conn.execute("SELECT id, batch_id FROM jobs ORDER BY id").fetchall()
    assert len(jobs) == 2
    assert jobs[0]["batch_id"] == jobs[1]["batch_id"]
    assert jobs[0]["batch_id"] is not None


def test_batch_requires_params_list_or_file(db_path: Path) -> None:
    with pytest.raises(SystemExit, match="params-list"):
        run(db_path, "batch", "m1")


def test_batch_rejects_empty_list(db_path: Path) -> None:
    with pytest.raises(SystemExit, match="non-empty"):
        run(db_path, "batch", "m1", "--params-list", "[]")


def test_admin_drain_and_resume_via_cli(db_path: Path) -> None:
    conn = db.connect(db_path)
    assert q.get_maintenance_mode(conn) is False

    run(db_path, "admin", "drain")
    assert q.get_maintenance_mode(conn) is True

    run(db_path, "admin", "resume")
    assert q.get_maintenance_mode(conn) is False


def test_dataset_writes_csv_to_file(db_path: Path, tmp_path: Path) -> None:
    run(db_path, "batch", "m1", "--params-list", '[{"pitch": "1[um]"}]')
    conn = db.connect(db_path)
    job = q.claim_next_job(conn, "worker-1")
    q.write_result(conn, job.id, "neff_real", 1.44)
    batch_id = conn.execute("SELECT batch_id FROM jobs WHERE id=?", (job.id,)).fetchone()["batch_id"]

    out_path = tmp_path / "out.csv"
    run(db_path, "dataset", batch_id, "--output", str(out_path))

    content = out_path.read_text()
    assert "pitch" in content
    assert "1.44" in content


def test_users_create_with_explicit_password(db_path: Path) -> None:
    run(db_path, "users", "create", "alice", "--password", "s3cret", "--admin")

    conn = db.connect(db_path)
    user = q.get_user_by_username(conn, "alice")
    assert user is not None
    assert user["is_admin"] == 1
    from simserver import auth

    assert auth.verify_password("s3cret", user["password_hash"])


def test_users_create_rejects_duplicate_username(db_path: Path) -> None:
    run(db_path, "users", "create", "alice", "--password", "s3cret")
    with pytest.raises(SystemExit, match="already exists"):
        run(db_path, "users", "create", "alice", "--password", "other")


def test_users_list(db_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run(db_path, "users", "create", "alice", "--password", "s3cret")
    run(db_path, "users", "create", "bob", "--password", "s3cret2")

    capsys.readouterr()
    run(db_path, "users", "list")
    out = capsys.readouterr().out
    assert "alice" in out
    assert "bob" in out
