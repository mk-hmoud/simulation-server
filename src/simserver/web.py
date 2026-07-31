"""Server-rendered web client (Jinja2 + minimal vanilla JS) for researchers
submitting simulations through a browser — a separate auth layer (session
cookies, per-user accounts) from the JSON API's static X-API-Key scheme.
Mounted under /ui by api.py, which owns the FastAPI `app` and static files.

Job/batch submission reuses the exact same manifest.validate_params /
resolve_outputs / queue.enqueue_job functions the JSON API route handlers
use — no parallel validation logic to keep in sync.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, Response
from fastapi import UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from . import auth
from . import dataset
from . import queue as q
from . import storage
from .deps import get_db, get_model_or_404, load_manifest
from .manifest import Manifest, ManifestValidationError, resolve_outputs, validate_params

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(prefix="/ui", tags=["web"])

_SESSION_COOKIE = "session"


def require_login(request: Request, conn: sqlite3.Connection = Depends(get_db)) -> sqlite3.Row:
    token = request.cookies.get(_SESSION_COOKIE)
    user = auth.get_session_user(conn, token) if token else None
    if user is None:
        # a redirect, not a 401 — this is a browser flow, not an API client
        raise HTTPException(status_code=303, headers={"Location": "/ui/login"})
    return user


def require_admin(user: sqlite3.Row = Depends(require_login)) -> sqlite3.Row:
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="admin access required")
    return user


# --- auth -------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_db),
):
    user = q.get_user_by_username(conn, username)
    if user is None or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html", {"error": "Invalid username or password"}, status_code=401
        )
    token = auth.create_session(conn, user["id"])
    response = RedirectResponse(url="/ui/", status_code=303)
    response.set_cookie(_SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=7 * 86400)
    return response


@router.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_db)):
    token = request.cookies.get(_SESSION_COOKIE)
    if token:
        auth.delete_session(conn, token)
    response = RedirectResponse(url="/ui/login", status_code=303)
    response.delete_cookie(_SESSION_COOKIE)
    return response


# --- dashboard / models -------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    summary = q.queue_summary(conn)
    recent_jobs = q.list_jobs(conn)[:20]
    return templates.TemplateResponse(
        request, "dashboard.html", {"user": user, "summary": summary, "recent_jobs": recent_jobs}
    )


def _owner_username(conn: sqlite3.Connection, owner_user_id: int | None) -> str | None:
    if owner_user_id is None:
        return None
    owner_row = q.get_user(conn, owner_user_id)
    return owner_row["username"] if owner_row else f"user#{owner_user_id}"


def _can_manage_model(user: sqlite3.Row, model_row: sqlite3.Row) -> bool:
    """Owner or admin. An ownerless model (registered via the CLI/JSON API,
    which have no per-caller identity) can only be managed by an admin."""
    return bool(user["is_admin"]) or model_row["owner_user_id"] == user["id"]


@router.get("/models", response_class=HTMLResponse)
def models_list(
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    models = []
    for row in q.list_models(conn):
        manifest = json.loads(row["manifest_json"])
        models.append(
            {
                "model_id": row["id"],
                "description": manifest.get("description", ""),
                "created_at": row["created_at"],
                "owner_username": _owner_username(conn, row["owner_user_id"]),
            }
        )
    return templates.TemplateResponse(request, "models_list.html", {"user": user, "models": models})


# NOTE: /models/new must be registered before /models/{model_id}, or FastAPI
# would match "new" as a model_id (literal path segments must come first)
@router.get("/models/new", response_class=HTMLResponse)
def model_new_form(request: Request, user: sqlite3.Row = Depends(require_login)):
    return templates.TemplateResponse(request, "model_new.html", {"user": user, "error": None})


@router.post("/models/new")
def model_new_submit(
    request: Request,
    manifest: str = Form(...),
    file: UploadFile = File(...),
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    try:
        parsed = Manifest.model_validate_json(manifest)
    except ValidationError as exc:
        return templates.TemplateResponse(
            request, "model_new.html", {"user": user, "error": str(exc)}, status_code=400
        )

    existing = q.get_model(conn, parsed.model_id)
    if existing is not None and not _can_manage_model(user, existing):
        owner = _owner_username(conn, existing["owner_user_id"]) or "an admin"
        return templates.TemplateResponse(
            request,
            "model_new.html",
            {
                "user": user,
                "error": f"model_id {parsed.model_id!r} already exists, owned by {owner} — "
                "pick a different model_id, or ask them (or an admin) to update it",
            },
            status_code=403,
        )

    mph_bytes = file.file.read()
    model_path = storage.save_model(parsed.model_id, mph_bytes, parsed)
    q.register_model(conn, parsed.model_id, str(model_path), parsed.model_dump(), owner_user_id=user["id"])
    return RedirectResponse(url=f"/ui/models/{parsed.model_id}", status_code=303)


@router.get("/models/{model_id}", response_class=HTMLResponse)
def model_detail(
    model_id: str,
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    model_row = get_model_or_404(conn, model_id)
    manifest = load_manifest(model_row)
    return templates.TemplateResponse(
        request,
        "model_detail.html",
        {
            "user": user,
            "model_id": model_id,
            "manifest": manifest,
            "owner_username": _owner_username(conn, model_row["owner_user_id"]),
            "can_manage": _can_manage_model(user, model_row),
            "error": None,
        },
    )


@router.post("/models/{model_id}/delete")
def delete_model_ui(
    model_id: str,
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    model_row = get_model_or_404(conn, model_id)
    if not _can_manage_model(user, model_row):
        raise HTTPException(status_code=403, detail="only the model's owner or an admin can delete it")

    manifest = load_manifest(model_row)
    try:
        q.delete_model(conn, model_id)
    except q.ModelInUseError as exc:
        return templates.TemplateResponse(
            request,
            "model_detail.html",
            {
                "user": user,
                "model_id": model_id,
                "manifest": manifest,
                "owner_username": _owner_username(conn, model_row["owner_user_id"]),
                "can_manage": True,
                "error": str(exc),
            },
            status_code=409,
        )
    storage.delete_model_files(model_id)
    return RedirectResponse(url="/ui/models", status_code=303)


@router.post("/models/{model_id}/jobs")
async def submit_job(
    model_id: str,
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    model_row = get_model_or_404(conn, model_id)
    manifest = load_manifest(model_row)
    form = await request.form()

    params: dict[str, str | list[str]] = {}
    for name in manifest.parameters:
        raw = form.get(name)
        if raw is None or not str(raw).strip():
            continue
        raw = str(raw).strip()
        if name == manifest.sweep_parameter and "," in raw:
            params[name] = [v.strip() for v in raw.split(",") if v.strip()]
        else:
            params[name] = raw

    requested_outputs = form.getlist("outputs") or None
    try:
        priority = int(form.get("priority") or 0)
    except ValueError:
        priority = 0

    try:
        validate_params(manifest, params)
        outputs = resolve_outputs(manifest, requested_outputs)
    except ManifestValidationError as exc:
        return templates.TemplateResponse(
            request,
            "model_detail.html",
            {
                "user": user,
                "model_id": model_id,
                "manifest": manifest,
                "owner_username": _owner_username(conn, model_row["owner_user_id"]),
                "can_manage": _can_manage_model(user, model_row),
                "error": str(exc),
            },
            status_code=400,
        )

    job_id = q.enqueue_job(
        conn, model_id, params, outputs=outputs, priority=priority, owner_user_id=user["id"]
    )
    return RedirectResponse(url=f"/ui/jobs/{job_id}", status_code=303)


@router.post("/models/{model_id}/batches")
def submit_batch(
    model_id: str,
    request: Request,
    params_list: str = Form(...),
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    model_row = get_model_or_404(conn, model_id)
    manifest = load_manifest(model_row)

    try:
        parsed_list = json.loads(params_list)
        if not isinstance(parsed_list, list) or not parsed_list:
            raise ValueError("must be a non-empty JSON array of parameter dicts")
        for params in parsed_list:
            validate_params(manifest, params)
        outputs = resolve_outputs(manifest, None)
    except (json.JSONDecodeError, ValueError, ManifestValidationError) as exc:
        return templates.TemplateResponse(
            request,
            "model_detail.html",
            {
                "user": user,
                "model_id": model_id,
                "manifest": manifest,
                "owner_username": _owner_username(conn, model_row["owner_user_id"]),
                "can_manage": _can_manage_model(user, model_row),
                "error": str(exc),
            },
            status_code=400,
        )

    batch_id = uuid.uuid4().hex
    for params in parsed_list:
        q.enqueue_job(conn, model_id, params, batch_id=batch_id, outputs=outputs, owner_user_id=user["id"])
    return RedirectResponse(url=f"/ui/batches/{batch_id}", status_code=303)


# --- jobs / batches ------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
def jobs_list(
    request: Request,
    mine: bool = False,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    jobs = q.list_jobs(conn, owner_user_id=user["id"] if mine else None)
    owner_names: dict[int, str] = {}

    def owner_name(owner_id: int | None) -> str | None:
        if owner_id is None:
            return None
        if owner_id not in owner_names:
            owner_row = q.get_user(conn, owner_id)
            owner_names[owner_id] = owner_row["username"] if owner_row else f"user#{owner_id}"
        return owner_names[owner_id]

    jobs_view = [{**dict(j), "owner_username": owner_name(j["owner_user_id"])} for j in jobs]
    return templates.TemplateResponse(request, "jobs_list.html", {"user": user, "jobs": jobs_view, "mine": mine})


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(
    job_id: int,
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    job = q.get_job(conn, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job_id {job_id}")
    results = q.list_results(conn, job_id)
    mode_selection = q.list_mode_selection(conn, job_id)

    chart_data: dict[str, list[dict]] = {}
    for row in results:
        chart_data.setdefault(row["output_name"], []).append(
            {"sweep_index": row["sweep_index"], "sweep_value": row["sweep_value"], "value_real": row["value_real"]}
        )
    is_sweep = len({row["sweep_index"] for row in results}) > 1

    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "user": user,
            "job": job,
            "results": results,
            "mode_selection": mode_selection,
            "is_sweep": is_sweep,
            "chart_data_json": json.dumps(chart_data),
        },
    )


@router.get("/batches/{batch_id}", response_class=HTMLResponse)
def batch_detail(
    batch_id: str,
    request: Request,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
):
    summary = q.batch_summary(conn, batch_id)
    if summary is None:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id!r}")
    jobs = q.list_batch_jobs(conn, batch_id)
    return templates.TemplateResponse(
        request, "batch_detail.html", {"user": user, "batch_id": batch_id, "summary": summary, "jobs": jobs}
    )


@router.get("/batches/{batch_id}/dataset.csv")
def batch_dataset_ui(
    batch_id: str,
    user: sqlite3.Row = Depends(require_login),
    conn: sqlite3.Connection = Depends(get_db),
) -> Response:
    # session-authenticated mirror of GET /batches/{id}/dataset.csv (which
    # needs the X-API-Key job key) — a logged-in browser session has neither
    # reason nor means to send that header
    if q.batch_summary(conn, batch_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown batch_id {batch_id!r}")
    return Response(content=dataset.batch_dataset_csv(conn, batch_id), media_type="text/csv")


# --- admin ----------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse)
def admin_page(
    request: Request,
    user: sqlite3.Row = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    return templates.TemplateResponse(request, "admin.html", {"user": user, "summary": q.queue_summary(conn)})


@router.post("/admin/drain")
def admin_drain_ui(
    user: sqlite3.Row = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    q.set_maintenance_mode(conn, True)
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.post("/admin/resume")
def admin_resume_ui(
    user: sqlite3.Row = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    q.set_maintenance_mode(conn, False)
    return RedirectResponse(url="/ui/admin", status_code=303)


@router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    user: sqlite3.Row = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    return templates.TemplateResponse(request, "users.html", {"user": user, "users": q.list_users(conn), "error": None})


@router.post("/users")
def users_create_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    is_admin: str | None = Form(None),  # unchecked checkboxes are simply absent from form data
    user: sqlite3.Row = Depends(require_admin),
    conn: sqlite3.Connection = Depends(get_db),
):
    if q.get_user_by_username(conn, username) is not None:
        return templates.TemplateResponse(
            request,
            "users.html",
            {"user": user, "users": q.list_users(conn), "error": f"user {username!r} already exists"},
            status_code=400,
        )
    q.create_user(conn, username, auth.hash_password(password), is_admin=is_admin is not None)
    return RedirectResponse(url="/ui/users", status_code=303)
