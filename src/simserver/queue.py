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
        row = conn.execute(
            """
            UPDATE jobs SET status='running', worker_id=?, started_at=?
            WHERE id = (
              SELECT id FROM jobs WHERE status='queued'
              ORDER BY (model_id = ?) DESC, priority DESC, created_at ASC
              LIMIT 1
            )
            RETURNING id, model_id, params_json, outputs_json, priority
            """,
            (worker_id, _now(), preferred_model_id),
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
