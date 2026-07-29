"""Job queue: model registration, enqueue, atomic claim, result writes.

The claim query is the plan §5.2 model-affinity-preferring atomic claim:
BEGIN IMMEDIATE + UPDATE...WHERE id = (SELECT ... ORDER BY affinity, priority,
age LIMIT 1) RETURNING id, all in one transaction, so two workers racing on
the same row can never both win it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from typing import Any


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def register_model(
    conn: sqlite3.Connection,
    model_id: str,
    path: str,
    manifest: dict[str, Any] | None = None,
) -> None:
    conn.execute(
        "INSERT INTO models (id, path, manifest_json, created_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET path=excluded.path, manifest_json=excluded.manifest_json",
        (model_id, path, json.dumps(manifest or {}), _now()),
    )


def get_model(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM models WHERE id = ?", (model_id,)).fetchone()


def list_models(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM models ORDER BY created_at").fetchall()


def enqueue_job(
    conn: sqlite3.Connection,
    model_id: str,
    params: dict[str, str],
    *,
    batch_id: str | None = None,
    outputs: dict[str, str] | None = None,
    priority: int = 0,
) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (batch_id, model_id, params_json, outputs_json, status, priority, created_at) "
        "VALUES (?, ?, ?, ?, 'queued', ?, ?)",
        (batch_id, model_id, json.dumps(params), json.dumps(outputs or {}), priority, _now()),
    )
    return cur.lastrowid


@dataclass
class ClaimedJob:
    id: int
    model_id: str
    params: dict[str, str]
    outputs: dict[str, str]
    priority: int


def claim_next_job(
    conn: sqlite3.Connection,
    worker_id: str,
    preferred_model_id: str | None = None,
) -> ClaimedJob | None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        now = _now()
        row = conn.execute(
            """
            UPDATE jobs SET status='running', worker_id=?, started_at=?
            WHERE id = (
              SELECT id FROM jobs
              WHERE status='queued' AND (not_before IS NULL OR not_before <= ?)
              ORDER BY (model_id = ?) DESC, priority DESC, created_at ASC
              LIMIT 1
            )
            RETURNING id, model_id, params_json, outputs_json, priority
            """,
            (worker_id, now, now, preferred_model_id),
        ).fetchone()
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")

    if row is None:
        return None
    return ClaimedJob(
        id=row["id"],
        model_id=row["model_id"],
        params=json.loads(row["params_json"]),
        outputs=json.loads(row["outputs_json"]) if row["outputs_json"] else {},
        priority=row["priority"],
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def list_results(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT output_name, sweep_index, sweep_value, value_real, value_imag "
        "FROM results WHERE job_id = ? ORDER BY output_name, sweep_index",
        (job_id,),
    ).fetchall()


def cancel_job(conn: sqlite3.Connection, job_id: int) -> bool:
    """Cancel a queued job. Returns False if the job doesn't exist or isn't queued.

    Only the cheap case (plan §7): a running job needs its worker process
    killed, which is supervisor territory (M5) and not implemented here.
    """
    cur = conn.execute(
        "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
        (_now(), job_id),
    )
    return cur.rowcount > 0


def queue_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    depth = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    }
    running = [
        dict(row)
        for row in conn.execute(
            "SELECT id AS job_id, model_id, worker_id, started_at FROM jobs WHERE status='running' "
            "ORDER BY started_at"
        )
    ]
    return {"depth": depth, "running": running}


def mark_done(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
        (_now(), job_id),
    )


def mark_failed(conn: sqlite3.Connection, job_id: int, error_class: str, error_message: str) -> None:
    conn.execute(
        "UPDATE jobs SET status='failed', finished_at=?, error_class=?, error_message=? WHERE id=?",
        (_now(), error_class, error_message, job_id),
    )


def list_running_jobs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, model_id, worker_id, started_at, attempt FROM jobs WHERE status='running'"
    ).fetchall()


def requeue_or_fail(
    conn: sqlite3.Connection,
    job_id: int,
    error_class: str,
    error_message: str,
    *,
    max_attempts: int = 2,
    backoff_seconds: float = 30.0,
) -> str:
    """Reclaim a job left 'running' by a dead or watchdog-killed worker (plan §6).

    Infrastructure failures retry with backoff, but capped at max_attempts
    total attempts — a job that reliably kills its worker (e.g. a genuinely
    hung solve) must not retry forever. Returns the resulting status, either
    'queued' (will retry after the backoff) or 'failed' (attempts exhausted).
    """
    row = conn.execute("SELECT attempt FROM jobs WHERE id=?", (job_id,)).fetchone()
    attempt = row["attempt"] if row else 1
    if attempt < max_attempts:
        not_before = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time() + backoff_seconds))
        conn.execute(
            "UPDATE jobs SET status='queued', worker_id=NULL, started_at=NULL, "
            "attempt=attempt+1, not_before=?, error_class=?, error_message=? WHERE id=?",
            (not_before, error_class, error_message, job_id),
        )
        return "queued"
    mark_failed(conn, job_id, error_class, error_message)
    return "failed"


def write_result(
    conn: sqlite3.Connection,
    job_id: int,
    output_name: str,
    value: complex | float,
    *,
    sweep_index: int = 0,
    sweep_value: float | None = None,
) -> None:
    if isinstance(value, complex):
        value_real, value_imag = value.real, value.imag
    else:
        value_real, value_imag = float(value), None
    conn.execute(
        "INSERT INTO results (job_id, output_name, sweep_index, sweep_value, value_real, value_imag) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (job_id, output_name, sweep_index, sweep_value, value_real, value_imag),
    )


def write_mode_selection(
    conn: sqlite3.Connection,
    job_id: int,
    strategy: str,
    n_modes_considered: int,
    *,
    sweep_index: int = 0,
    core_fraction: float | None = None,
) -> None:
    conn.execute(
        "INSERT INTO mode_selection (job_id, sweep_index, strategy, core_fraction, n_modes_considered) "
        "VALUES (?, ?, ?, ?, ?)",
        (job_id, sweep_index, strategy, core_fraction, n_modes_considered),
    )


def list_mode_selection(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT sweep_index, strategy, core_fraction, n_modes_considered FROM mode_selection "
        "WHERE job_id = ? ORDER BY sweep_index",
        (job_id,),
    ).fetchall()
