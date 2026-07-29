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
