"""M4 HTTP API (plan §7): models, jobs, results, queue. No mph import here —
the api process never touches COMSOL, only the worker does (plan §2).
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ValidationError

from . import config, db
from . import queue as q
from . import storage
from .manifest import Manifest, ManifestValidationError, resolve_outputs, validate_params

app = FastAPI(title="simulation-server")


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect(config.DB_PATH)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def _load_manifest(row: sqlite3.Row) -> Manifest:
    return Manifest.model_validate(json.loads(row["manifest_json"]))


def _get_model_or_404(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row:
    row = q.get_model(conn, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown model_id {model_id!r}")
    return row


class JobCreate(BaseModel):
    model_id: str
    # a list value is a sweep request (plan §7) — only valid for the
    # manifest's sweep_parameter, enforced by validate_params
    params: dict[str, str | list[str]] = {}
    outputs: list[str] | None = None
    priority: int = 0


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/models")
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


@app.get("/models")
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


@app.get("/models/{model_id}")
def get_model(model_id: str, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = _get_model_or_404(conn, model_id)
    return json.loads(row["manifest_json"])


@app.post("/jobs")
def create_job(job: JobCreate, conn: sqlite3.Connection = Depends(get_db)) -> dict[str, int]:
    model_row = _get_model_or_404(conn, job.model_id)
    manifest = _load_manifest(model_row)

    try:
        validate_params(manifest, job.params)
        outputs = resolve_outputs(manifest, job.outputs)
    except ManifestValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    job_id = q.enqueue_job(conn, job.model_id, job.params, outputs=outputs, priority=job.priority)
    return {"job_id": job_id}


@app.get("/jobs/{job_id}")
def get_job(job_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    row = q.get_job(conn, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    return dict(row)


@app.get("/jobs/{job_id}/results")
def get_job_results(job_id: int, conn: sqlite3.Connection = Depends(get_db)) -> dict:
    if q.get_job(conn, job_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    return {"job_id": job_id, "results": [dict(row) for row in q.list_results(conn, job_id)]}


@app.delete("/jobs/{job_id}")
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


@app.get("/queue")
def get_queue(conn: sqlite3.Connection = Depends(get_db)) -> dict:
    return q.queue_summary(conn)
