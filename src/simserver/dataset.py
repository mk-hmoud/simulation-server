"""Batch dataset flattening (plan §8): "an entire batch flattens to a
dataframe in one query" is the actual point of the system if it feeds ML
work — this is that flattening, as CSV.

One row per (job_id, sweep_index): scalar params used by that job (list-
valued sweep params are excluded — sweep_value already carries that), plus
one column per output (`<name>`, and `<name>__imag` only if any row in the
batch actually has a non-null imaginary part for that output, to keep plain
real-valued datasets clean).
"""

from __future__ import annotations

import csv
import io
import json
import sqlite3

from . import queue as q


def _scalar_params(params_json: str) -> dict[str, str]:
    params = json.loads(params_json)
    return {name: value for name, value in params.items() if not isinstance(value, list)}


def batch_dataset_rows(conn: sqlite3.Connection, batch_id: str) -> tuple[list[dict], list[str]]:
    """Returns (rows, fieldnames) for the batch, or ([], []) if the batch is
    unknown or none of its jobs have results yet."""
    jobs = q.list_batch_jobs(conn, batch_id)
    if not jobs:
        return [], []

    param_names: set[str] = set()
    output_names: set[str] = set()
    output_has_imag: set[str] = set()
    per_point: dict[tuple[int, int], dict] = {}

    for job in jobs:
        scalar_params = _scalar_params(job["params_json"])
        param_names.update(scalar_params)
        for row in q.list_results(conn, job["id"]):
            key = (job["id"], row["sweep_index"])
            entry = per_point.setdefault(
                key,
                {"job_id": job["id"], "sweep_index": row["sweep_index"], "sweep_value": row["sweep_value"], **scalar_params},
            )
            entry[row["output_name"]] = row["value_real"]
            output_names.add(row["output_name"])
            if row["value_imag"] is not None:
                entry[f"{row['output_name']}__imag"] = row["value_imag"]
                output_has_imag.add(row["output_name"])

    if not per_point:
        return [], []

    output_columns: list[str] = []
    for name in sorted(output_names):
        output_columns.append(name)
        if name in output_has_imag:
            output_columns.append(f"{name}__imag")

    fieldnames = ["job_id", "sweep_index", "sweep_value", *sorted(param_names), *output_columns]
    rows = [per_point[key] for key in sorted(per_point)]
    return rows, fieldnames


def batch_dataset_csv(conn: sqlite3.Connection, batch_id: str) -> str:
    rows, fieldnames = batch_dataset_rows(conn, batch_id)
    if not fieldnames:
        return ""
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, restval="")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buffer.getvalue()
