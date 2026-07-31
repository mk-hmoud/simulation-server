from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simserver import config, db
from simserver.api import app, get_db, require_admin_key, require_job_key


def test_require_job_key_disabled_when_no_keys_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOB_API_KEY", None)
    monkeypatch.setattr(config, "ADMIN_API_KEY", None)
    require_job_key(x_api_key=None)  # must not raise


def test_require_job_key_accepts_either_configured_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOB_API_KEY", "job-secret")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "admin-secret")
    require_job_key(x_api_key="job-secret")
    require_job_key(x_api_key="admin-secret")


def test_require_job_key_rejects_missing_or_wrong_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOB_API_KEY", "job-secret")
    monkeypatch.setattr(config, "ADMIN_API_KEY", None)
    with pytest.raises(Exception) as exc_info:
        require_job_key(x_api_key=None)
    assert exc_info.value.status_code == 401
    with pytest.raises(Exception):
        require_job_key(x_api_key="wrong")


def test_require_admin_key_disabled_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "ADMIN_API_KEY", None)
    require_admin_key(x_api_key=None)  # must not raise


def test_require_admin_key_rejects_job_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "JOB_API_KEY", "job-secret")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "admin-secret")
    with pytest.raises(Exception) as exc_info:
        require_admin_key(x_api_key="job-secret")
    assert exc_info.value.status_code == 401
    require_admin_key(x_api_key="admin-secret")  # must not raise


@pytest.fixture
def authed_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    monkeypatch.setattr(config, "JOB_API_KEY", "job-secret")
    monkeypatch.setattr(config, "ADMIN_API_KEY", "admin-secret")

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


def test_healthz_never_requires_a_key(authed_client: TestClient) -> None:
    assert authed_client.get("/healthz").status_code == 200


def test_queue_requires_job_key(authed_client: TestClient) -> None:
    assert authed_client.get("/queue").status_code == 401
    assert authed_client.get("/queue", headers={"x-api-key": "wrong"}).status_code == 401
    assert authed_client.get("/queue", headers={"x-api-key": "job-secret"}).status_code == 200
    assert authed_client.get("/queue", headers={"x-api-key": "admin-secret"}).status_code == 200


def test_create_model_requires_admin_key_not_job_key(authed_client: TestClient) -> None:
    manifest = {"model_id": "m1", "parameters": {}}
    files = {"file": ("model.mph", b"stub")}

    response = authed_client.post("/models", data={"manifest": json.dumps(manifest)}, files=files)
    assert response.status_code == 401

    response = authed_client.post(
        "/models", data={"manifest": json.dumps(manifest)}, files=files, headers={"x-api-key": "job-secret"}
    )
    assert response.status_code == 401

    response = authed_client.post(
        "/models", data={"manifest": json.dumps(manifest)}, files=files, headers={"x-api-key": "admin-secret"}
    )
    assert response.status_code == 200


def test_admin_drain_requires_admin_key(authed_client: TestClient) -> None:
    assert authed_client.post("/admin/drain", headers={"x-api-key": "job-secret"}).status_code == 401
    assert authed_client.post("/admin/drain", headers={"x-api-key": "admin-secret"}).status_code == 200
