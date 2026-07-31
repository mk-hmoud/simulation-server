from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from simserver import auth, config, db
from simserver import queue as q
from simserver.api import app
from simserver.deps import get_db

MANIFEST = {
    "model_id": "m1",
    "description": "test model",
    "parameters": {
        "l": {"unit": "um", "min": 0.5, "max": 2.0, "geometry": False},
        "p": {"unit": "um", "min": 1.0, "max": 3.0, "geometry": True},
    },
    "sweep_parameter": "l",
    "outputs": {"neff_real": "real(emw.neff)"},
}


@pytest.fixture
def app_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
        yield TestClient(app), tmp_path
    finally:
        app.dependency_overrides.clear()


def make_conn(tmp_path: Path):
    conn = db.connect(tmp_path / "jobs.db")
    db.init_db(conn)
    return conn


def login(client: TestClient, conn, *, username="alice", password="pw", admin=False) -> int:
    user_id = q.create_user(conn, username, auth.hash_password(password), is_admin=admin)
    response = client.post("/ui/login", data={"username": username, "password": password})
    assert response.status_code == 200
    return user_id


# --- auth flow ------------------------------------------------------------


def test_unauthenticated_dashboard_redirects_to_login(app_client) -> None:
    client, _ = app_client
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/login"


def test_login_with_wrong_password_shows_error(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.create_user(conn, "alice", auth.hash_password("correct"))

    response = client.post("/ui/login", data={"username": "alice", "password": "wrong"})
    assert response.status_code == 401
    assert "Invalid username or password" in response.text


def test_login_success_sets_session_cookie_and_redirects(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.create_user(conn, "alice", auth.hash_password("correct"))

    response = client.post(
        "/ui/login", data={"username": "alice", "password": "correct"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/ui/"
    assert "session" in response.cookies


def test_login_then_dashboard_accessible(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn)

    response = client.get("/ui/")
    assert response.status_code == 200
    assert "Dashboard" in response.text


def test_logout_clears_session(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn)

    client.post("/ui/logout")
    response = client.get("/ui/", follow_redirects=False)
    assert response.status_code == 303


# --- job/batch submission via forms ----------------------------------------


def test_submit_job_via_form_sets_owner_and_creates_job(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    user_id = login(client, conn)

    response = client.post(
        "/ui/models/m1/jobs",
        data={"l": "0.9[um]", "priority": "0", "outputs": ["neff_real"]},
        follow_redirects=False,
    )
    assert response.status_code == 303
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])

    row = conn.execute("SELECT owner_user_id, model_id FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert row["owner_user_id"] == user_id
    assert row["model_id"] == "m1"


def test_submit_job_with_comma_separated_sweep_values(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)

    response = client.post(
        "/ui/models/m1/jobs",
        data={"l": "0.6[um], 0.8[um], 1.0[um]", "outputs": ["neff_real"]},
        follow_redirects=False,
    )
    job_id = int(response.headers["location"].rsplit("/", 1)[-1])
    row = conn.execute("SELECT params_json FROM jobs WHERE id=?", (job_id,)).fetchone()
    assert json.loads(row["params_json"])["l"] == ["0.6[um]", "0.8[um]", "1.0[um]"]


def test_submit_job_rejects_out_of_range_param_same_validation_as_api(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)

    response = client.post("/ui/models/m1/jobs", data={"l": "9999[um]"})
    assert response.status_code == 400
    assert "outside allowed range" in response.text


def test_submit_batch_via_form_sets_owner_on_every_job(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    user_id = login(client, conn)

    response = client.post(
        "/ui/models/m1/batches",
        data={"params_list": json.dumps([{"p": "1.5[um]"}, {"p": "2.0[um]"}])},
        follow_redirects=False,
    )
    assert response.status_code == 303
    batch_id = response.headers["location"].rsplit("/", 1)[-1]
    jobs = q.list_batch_jobs(conn, batch_id)
    assert len(jobs) == 2
    assert all(j["owner_user_id"] == user_id for j in jobs)


def test_submit_batch_rejects_invalid_json(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)

    response = client.post("/ui/models/m1/batches", data={"params_list": "not json"})
    assert response.status_code == 400


def test_batch_dataset_csv_via_session_auth(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)

    response = client.post(
        "/ui/models/m1/batches",
        data={"params_list": json.dumps([{"p": "1.5[um]"}])},
        follow_redirects=False,
    )
    batch_id = response.headers["location"].rsplit("/", 1)[-1]
    job = q.claim_next_job(conn, "worker-1")
    q.write_result(conn, job.id, "neff_real", 1.44)

    csv_response = client.get(f"/ui/batches/{batch_id}/dataset.csv")
    assert csv_response.status_code == 200
    assert "neff_real" in csv_response.text
    assert "1.44" in csv_response.text


# --- pages ------------------------------------------------------------------


def test_models_list_and_detail_pages(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)

    assert "m1" in client.get("/ui/models").text
    detail = client.get("/ui/models/m1")
    assert detail.status_code == 200
    assert "Submit a job" in detail.text


def test_unknown_model_detail_is_404(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn)
    assert client.get("/ui/models/does-not-exist").status_code == 404


def test_job_detail_page_renders(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    login(client, conn)
    job_id = q.enqueue_job(conn, "m1", {})

    response = client.get(f"/ui/jobs/{job_id}")
    assert response.status_code == 200
    assert f"Job {job_id}" in response.text


def test_unknown_job_detail_is_404(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn)
    assert client.get("/ui/jobs/999").status_code == 404


def test_jobs_list_mine_filter(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "m1", str(tmp_path / "model.mph"), MANIFEST)
    alice_id = login(client, conn, username="alice")
    q.enqueue_job(conn, "m1", {}, owner_user_id=alice_id)
    bob_id = q.create_user(conn, "bob", auth.hash_password("pw"))
    q.enqueue_job(conn, "m1", {}, owner_user_id=bob_id)

    all_jobs = client.get("/ui/jobs").text
    assert "alice" in all_jobs
    assert "bob" in all_jobs

    mine = client.get("/ui/jobs?mine=true").text
    assert "alice" in mine
    assert "bob" not in mine


# --- admin gating -----------------------------------------------------------


def test_non_admin_blocked_from_admin_page(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=False)
    assert client.get("/ui/admin").status_code == 403


def test_admin_can_access_admin_page(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)
    assert client.get("/ui/admin").status_code == 200


def test_non_admin_blocked_from_users_page(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=False)
    assert client.get("/ui/users").status_code == 403


def test_non_admin_can_register_a_new_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    user_id = login(client, conn, admin=False)

    assert client.get("/ui/models/new").status_code == 200
    response = client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "x", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    row = q.get_model(conn, "x")
    assert row is not None
    assert row["owner_user_id"] == user_id


def test_non_owner_non_admin_cannot_overwrite_someone_elses_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "shared", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )
    client.post("/ui/logout")
    login(client, conn, username="bob", admin=False)

    response = client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "shared", "parameters": {}})},
        files={"file": ("model.mph", b"stub2")},
    )
    assert response.status_code == 403
    # must not have overwritten alice's file
    assert json.loads(conn.execute("SELECT manifest_json FROM models WHERE id='shared'").fetchone()[0])


def test_owner_can_reregister_their_own_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "mine", "description": "v1", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )

    response = client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "mine", "description": "v2", "parameters": {}})},
        files={"file": ("model.mph", b"stub2")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    manifest = json.loads(q.get_model(conn, "mine")["manifest_json"])
    assert manifest["description"] == "v2"


def test_admin_can_overwrite_anyones_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "shared2", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )
    client.post("/ui/logout")
    login(client, conn, username="admin", admin=True)

    response = client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "shared2", "description": "fixed", "parameters": {}})},
        files={"file": ("model.mph", b"stub2")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    # ownership stays with alice even though admin updated it
    assert q.get_model(conn, "shared2")["owner_user_id"] is not None


def test_owner_can_delete_their_own_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "deleteme", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )

    response = client.post("/ui/models/deleteme/delete", follow_redirects=False)
    assert response.status_code == 303
    assert q.get_model(conn, "deleteme") is None


def test_non_owner_non_admin_cannot_delete_someone_elses_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "notyours", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )
    client.post("/ui/logout")
    login(client, conn, username="bob", admin=False)

    response = client.post("/ui/models/notyours/delete")
    assert response.status_code == 403
    assert q.get_model(conn, "notyours") is not None


def test_deleting_a_model_with_jobs_shows_clean_error_not_crash(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, username="alice", admin=False)
    client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "inuse", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
    )
    client.post("/ui/models/inuse/jobs", data={})

    response = client.post("/ui/models/inuse/delete")
    assert response.status_code == 409
    assert "still has jobs referencing it" in response.text
    assert q.get_model(conn, "inuse") is not None  # not deleted


def test_admin_can_delete_ownerless_model(app_client) -> None:
    """A model registered via the CLI/JSON API has no owner (no per-caller
    identity there) — only an admin can manage it through the web UI."""
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "cli_model", str(tmp_path / "m.mph"), {"model_id": "cli_model", "parameters": {}})
    login(client, conn, username="admin", admin=True)

    response = client.post("/ui/models/cli_model/delete", follow_redirects=False)
    assert response.status_code == 303
    assert q.get_model(conn, "cli_model") is None


def test_non_admin_cannot_delete_ownerless_model(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    q.register_model(conn, "cli_model2", str(tmp_path / "m.mph"), {"model_id": "cli_model2", "parameters": {}})
    login(client, conn, username="researcher", admin=False)

    response = client.post("/ui/models/cli_model2/delete")
    assert response.status_code == 403
    assert q.get_model(conn, "cli_model2") is not None


def test_admin_drain_and_resume_via_ui(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)

    client.post("/ui/admin/drain")
    assert q.get_maintenance_mode(conn) is True

    client.post("/ui/admin/resume")
    assert q.get_maintenance_mode(conn) is False


def test_admin_register_model_via_ui(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)

    response = client.post(
        "/ui/models/new",
        data={"manifest": json.dumps({"model_id": "new_model", "parameters": {}})},
        files={"file": ("model.mph", b"stub")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert q.get_model(conn, "new_model") is not None


def test_admin_create_user_via_ui(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)

    response = client.post(
        "/ui/users", data={"username": "bob", "password": "pw2"}, follow_redirects=False
    )
    assert response.status_code == 303
    bob = q.get_user_by_username(conn, "bob")
    assert bob is not None
    assert bob["is_admin"] == 0


def test_admin_create_admin_user_via_checkbox(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)

    client.post("/ui/users", data={"username": "carol", "password": "pw3", "is_admin": "on"})
    carol = q.get_user_by_username(conn, "carol")
    assert carol["is_admin"] == 1


def test_create_duplicate_username_via_ui_shows_error(app_client) -> None:
    client, tmp_path = app_client
    conn = make_conn(tmp_path)
    login(client, conn, admin=True)
    q.create_user(conn, "dup", auth.hash_password("x"))

    response = client.post("/ui/users", data={"username": "dup", "password": "y"})
    assert response.status_code == 400
    assert "already exists" in response.text
