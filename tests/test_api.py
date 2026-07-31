from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simserver import config, db
from simserver.api import app, get_db

MANIFEST = {
    "model_id": "spr_pcf_v1",
    "description": "Gold-coated SPR-PCF, D-shape channel",
    "study": "std1",
    "parameters": {
        "lambda0": {"unit": "nm", "min": 500, "max": 2000, "geometry": False},
        "pitch": {"unit": "um", "min": 1.0, "max": 5.0, "geometry": True},
    },
    "sweep_parameter": "lambda0",
    "outputs": {
        "neff_real": "real(emw.neff)",
        "neff_imag": "imag(emw.neff)",
    },
    "exports": [],
}


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")

    def override_get_db():
        conn = db.connect(tmp_path / "jobs.db")
        db.init_db(conn)
        try:
            yield conn
        finally:
            conn.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def register_model(client: TestClient, manifest: dict = MANIFEST) -> None:
    response = client.post(
        "/models",
        data={"manifest": json.dumps(manifest)},
        files={"file": ("model.mph", b"not a real model, just needs to exist")},
    )
    assert response.status_code == 200, response.text


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_register_and_list_and_get_model(client: TestClient) -> None:
    register_model(client)

    listed = client.get("/models").json()
    assert listed == [
        {"model_id": "spr_pcf_v1", "description": MANIFEST["description"], "created_at": listed[0]["created_at"]}
    ]

    fetched = client.get("/models/spr_pcf_v1").json()
    assert fetched["outputs"] == MANIFEST["outputs"]


def test_get_unknown_model_is_404(client: TestClient) -> None:
    assert client.get("/models/does-not-exist").status_code == 404


def test_invalid_manifest_is_rejected(client: TestClient) -> None:
    bad = {"model_id": "x"}  # missing required "parameters"
    response = client.post(
        "/models",
        data={"manifest": json.dumps(bad)},
        files={"file": ("model.mph", b"stub")},
    )
    assert response.status_code == 400


def test_create_job_happy_path(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {"lambda0": "1550[nm]"}},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]

    job = client.get(f"/jobs/{job_id}").json()
    assert job["status"] == "queued"
    assert job["model_id"] == "spr_pcf_v1"


def test_create_job_for_unknown_model_is_404(client: TestClient) -> None:
    response = client.post("/jobs", json={"model_id": "nope", "params": {}})
    assert response.status_code == 404


def test_create_job_rejects_unknown_parameter(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {"not_a_real_param": "1[um]"}},
    )
    assert response.status_code == 400
    assert "unknown parameter" in response.json()["detail"]


def test_create_job_rejects_out_of_range_parameter(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {"lambda0": "9999[nm]"}},
    )
    assert response.status_code == 400
    assert "outside allowed range" in response.json()["detail"]


def test_create_job_accepts_sweep_list_for_sweep_parameter(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={
            "model_id": "spr_pcf_v1",
            "params": {"lambda0": ["1500[nm]", "1550[nm]", "1600[nm]"]},
        },
    )
    assert response.status_code == 200, response.text

    from simserver import queue as q

    override = app.dependency_overrides[get_db]
    conn_gen = override()
    conn = next(conn_gen)
    job = q.claim_next_job(conn, "worker-1")
    assert job.params == {"lambda0": ["1500[nm]", "1550[nm]", "1600[nm]"]}


def test_create_job_rejects_sweep_list_for_non_sweep_parameter(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {"pitch": ["1.5[um]", "2.0[um]"]}},
    )
    assert response.status_code == 400
    assert "sweep_parameter" in response.json()["detail"]


def test_create_job_rejects_unknown_output_name(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {}, "outputs": ["not_a_real_output"]},
    )
    assert response.status_code == 400
    assert "unknown output" in response.json()["detail"]


def test_job_outputs_are_resolved_from_manifest_not_client(client: TestClient) -> None:
    """The client can only pick output *names*; the expression always comes
    from the manifest — this is the RCE-prevention boundary from plan §4."""
    register_model(client)
    response = client.post(
        "/jobs",
        json={"model_id": "spr_pcf_v1", "params": {}, "outputs": ["neff_real"]},
    )
    job_id = response.json()["job_id"]

    from simserver import queue as q

    override = app.dependency_overrides[get_db]
    conn_gen = override()
    conn = next(conn_gen)
    job = q.claim_next_job(conn, "worker-1")
    assert job.id == job_id
    assert job.outputs == {"neff_real": "real(emw.neff)"}


def test_get_unknown_job_is_404(client: TestClient) -> None:
    assert client.get("/jobs/999").status_code == 404
    assert client.get("/jobs/999/results").status_code == 404


def test_job_results_empty_until_processed(client: TestClient) -> None:
    register_model(client)
    job_id = client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}}).json()["job_id"]
    assert client.get(f"/jobs/{job_id}/results").json() == {"job_id": job_id, "results": []}


def test_cancel_queued_job(client: TestClient) -> None:
    register_model(client)
    job_id = client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}}).json()["job_id"]

    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 200
    assert client.get(f"/jobs/{job_id}").json()["status"] == "cancelled"


def test_cancel_running_job_is_conflict(client: TestClient) -> None:
    register_model(client)
    job_id = client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}}).json()["job_id"]

    from simserver import queue as q

    override = app.dependency_overrides[get_db]
    conn_gen = override()
    conn = next(conn_gen)
    q.claim_next_job(conn, "worker-1")

    response = client.delete(f"/jobs/{job_id}")
    assert response.status_code == 409


def test_queue_summary(client: TestClient) -> None:
    register_model(client)
    client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}})
    client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}})

    summary = client.get("/queue").json()
    assert summary["depth"] == {"queued": 2}
    assert summary["running"] == []
    assert summary["maintenance_mode"] is False


def test_create_batch_enqueues_one_job_per_params_entry(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/batches",
        json={
            "model_id": "spr_pcf_v1",
            "params_list": [{"pitch": "1.5[um]"}, {"pitch": "2.0[um]"}, {"pitch": "2.5[um]"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["job_ids"]) == 3

    summary = client.get(f"/batches/{body['batch_id']}").json()
    assert summary["total"] == 3
    assert summary["depth"] == {"queued": 3}


def test_create_batch_rejects_empty_params_list(client: TestClient) -> None:
    register_model(client)
    response = client.post("/batches", json={"model_id": "spr_pcf_v1", "params_list": []})
    assert response.status_code == 400


def test_create_batch_validates_every_entry(client: TestClient) -> None:
    register_model(client)
    response = client.post(
        "/batches",
        json={"model_id": "spr_pcf_v1", "params_list": [{"pitch": "1.5[um]"}, {"pitch": "9999[um]"}]},
    )
    assert response.status_code == 400
    assert "outside allowed range" in response.json()["detail"]


def test_create_batch_for_unknown_model_is_404(client: TestClient) -> None:
    response = client.post("/batches", json={"model_id": "nope", "params_list": [{}]})
    assert response.status_code == 404


def test_get_unknown_batch_is_404(client: TestClient) -> None:
    assert client.get("/batches/does-not-exist").status_code == 404
    assert client.get("/batches/does-not-exist/dataset.csv").status_code == 404


def test_batch_dataset_csv_reflects_written_results(client: TestClient) -> None:
    register_model(client)
    batch_id = client.post(
        "/batches",
        json={"model_id": "spr_pcf_v1", "params_list": [{"pitch": "1.5[um]"}]},
    ).json()["batch_id"]

    from simserver import queue as q

    override = app.dependency_overrides[get_db]
    conn_gen = override()
    conn = next(conn_gen)
    job = q.claim_next_job(conn, "worker-1")
    q.write_result(conn, job.id, "neff_real", 1.44)

    response = client.get(f"/batches/{batch_id}/dataset.csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "pitch" in response.text
    assert "1.44" in response.text


def test_admin_drain_and_resume_toggle_maintenance_mode(client: TestClient) -> None:
    assert client.get("/queue").json()["maintenance_mode"] is False

    response = client.post("/admin/drain")
    assert response.status_code == 200
    assert response.json()["maintenance_mode"] is True
    assert client.get("/queue").json()["maintenance_mode"] is True

    response = client.post("/admin/resume")
    assert response.json()["maintenance_mode"] is False
    assert client.get("/queue").json()["maintenance_mode"] is False


def test_jobs_are_not_claimable_while_drained(client: TestClient) -> None:
    register_model(client)
    client.post("/jobs", json={"model_id": "spr_pcf_v1", "params": {}})
    client.post("/admin/drain")

    from simserver import queue as q

    override = app.dependency_overrides[get_db]
    conn_gen = override()
    conn = next(conn_gen)
    assert q.claim_next_job(conn, "worker-1") is None

    client.post("/admin/resume")
    assert q.claim_next_job(conn, "worker-1") is not None
