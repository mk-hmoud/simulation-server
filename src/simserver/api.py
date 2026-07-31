"""M4 HTTP API (plan §7): models, jobs, results, queue. No mph import here —
the api process never touches COMSOL, only the worker does (plan §2).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import config
from . import dataset
from . import queue as q
from . import storage
from . import web
from .deps import get_db, get_model_or_404, load_manifest
from .manifest import Manifest, ManifestValidationError, resolve_outputs, validate_params

app = FastAPI(title="simulation-server")
app.include_router(web.router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")


def require_job_key(x_api_key: str | None = Header(default=None)) -> None:
    """Plan §9: a static API key on top of the tunnel, for the case where the
    tunnel is shared. Either configured key satisfies this tier — admin can
    do everything a job key can. If NEITHER key is configured, auth is
    disabled entirely (dev mode); production deployments must set both env
    vars (SIMSERVER_JOB_API_KEY, SIMSERVER_ADMIN_API_KEY)."""
    configured = {k for k in (config.JOB_API_KEY, config.ADMIN_API_KEY) if k}
    if not configured:
        return
    if x_api_key in configured:
        return
    raise HTTPException(status_code=401, detail="missing or invalid API key")


def require_admin_key(x_api_key: str | None = Header(default=None)) -> None:
    """Model upload and maintenance-mode toggling need the admin key
    specifically, never the job key (plan §9). Disabled (dev mode) if
    SIMSERVER_ADMIN_API_KEY isn't set."""
    if config.ADMIN_API_KEY is None:
        return
    if x_api_key == config.ADMIN_API_KEY:
        return
    raise HTTPException(status_code=401, detail="missing or invalid admin API key")


class JobCreate(BaseModel):
    model_id: str
    # a list value is a sweep request (plan §7) — only valid for the
    # manifest's sweep_parameter, enforced by validate_params
    params: dict[str, str | list[str]] = {}
    outputs: list[str] | None = None
    priority: int = 0


class BatchCreate(BaseModel):
    model_id: str
    # one job per entry (plan §7: batches are for geometry variation, where a
    # rebuild is unavoidable anyway — unlike a sweep, which is one job)
    params_list: list[dict[str, str | list[str]]]
    outputs: list[str] | None = None
    priority: int = 0


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/models", dependencies=[Depends(require_admin_key)])
def create_model(
    manifest: str = Form(..., description="JSON manifest, see plan §4"),
    file: UploadFile = File(...),
    conn: sqlite3.Connection = Depends(get_db),
) -> dict[str, str]:
    try:
        parsed = Manifest.model_validate_json(manifest)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=f"invalid manifest: {exc}") from exc

    mph_bytes = file.file.read()
    model_path = storage.save_model(parsed.model_id, mph_bytes, parsed)
    q.register_model(conn, parsed.model_id, str(model_path), parsed.model_dump())
    return {"model_id": parsed.model_id, "path": str(model_path)}


@app.get("/models", dependencies=[Depends(require_job_key)])
def list_models(conn: sqlite3.Connection = Depends(get_db)) -> list[dict]:
    out = []
    for row in q.list_models(conn):
        manifest = json.loads(row["manifest_json"])
        out.append(
            {
                "model_id": row["id"],
                "description": manifest.get("description", ""),
                "created_at": row["created_at"],
            }
        )
    return out


@app.get("/models/{model_id}", dependencies=[Depends(require_job_key)])
def get_model(model_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = get_model_or_404(conn, model_id)
    return json.loads(row["manifest_json"])


@app.post("/jobs", dependencies=[Depends(require_job_key)])
def create_job(job: JobCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, int]:
    model_row = get_model_or_404(conn, job.model_id)
    manifest = load_manifest(model_row)

    try:
        validate_params(manifest, job.params)
        outputs = resolve_outputs(manifest, job.outputs)
    except ManifestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = q.enqueue_job(conn, job.model_id, job.params, outputs=outputs, priority=job.priority)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}", dependencies=[Depends(require_job_key)])
def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = q.get_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    return dict(row)


@app.get("/jobs/{job_id}/results", dependencies=[Depends(require_job_key)])
def get_job_results(job_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if q.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    return {"job_id": job_id, "results": [dict(row) for row in q.list_results(conn, job_id)]}


@app.delete("/jobs/{job_id}", dependencies=[Depends(require_job_key)])
def cancel_job(job_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = q.get_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    if row["status"] != "queued":
        raise HTTPException(
            status_code=409,
            detail=f"cannot cancel job in status {row['status']!r} "
            "(killing a running job needs the supervisor, not implemented until M5)",
        )
    q.cancel_job(conn, job_id)
    return {"job_id": job_id, "status": "cancelled"}


@app.get("/queue", dependencies=[Depends(require_job_key)])
def get_queue(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return q.queue_summary(conn)


@app.post("/batches", dependencies=[Depends(require_job_key)])
def create_batch(batch: BatchCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    model_row = get_model_or_404(conn, batch.model_id)
    manifest = load_manifest(model_row)

    if not batch.params_list:
        raise HTTPException(status_code=400, detail="params_list must not be empty")

    try:
        for params in batch.params_list:
            validate_params(manifest, params)
        outputs = resolve_outputs(manifest, batch.outputs)
    except ManifestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    batch_id = uuid.uuid4().hex
    job_ids = [
        q.enqueue_job(conn, batch.model_id, params, batch_id=batch_id, outputs=outputs, priority=batch.priority)
        for params in batch.params_list
    ]
    return {"batch_id": batch_id, "job_ids": job_ids}


@app.get("/batches/{batch_id}", dependencies=[Depends(require_job_key)])
def get_batch(batch_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    summary = q.batch_summary(conn, batch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id!r}")
    return summary


@app.get("/batches/{batch_id}/dataset.csv", dependencies=[Depends(require_job_key)])
def get_batch_dataset(batch_id: str, conn: sqlite3.Connection = Depends(get_db)) -> Response:
    if q.batch_summary(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id!r}")
    csv_text = dataset.batch_dataset_csv(conn, batch_id)
    return Response(content=csv_text, media_type="text/csv")


@app.post("/admin/drain", dependencies=[Depends(require_admin_key)])
def admin_drain(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    """Plan §7 maintenance mode: stops workers claiming new jobs and, once
    each finishes its current job (if any), exits — actually releasing the
    license, not just idling. Poll GET /queue's "running" list to see when
    the seat is actually clear."""
    q.set_maintenance_mode(conn, True)
    return q.queue_summary(conn)


@app.post("/admin/resume", dependencies=[Depends(require_admin_key)])
def admin_resume(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    q.set_maintenance_mode(conn, False)
    return q.queue_summary(conn)
