"""Shared FastAPI dependencies for api.py and web.py.

Kept in its own module (rather than defined in api.py, where they used to
live) specifically so web.py can import them without api.py having to
import web.py first — api.py mounts web.py's router, so the reverse import
would be circular.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Iterator

from fastapi import HTTPException

from . import config, db
from . import queue as q
from .manifest import Manifest


def get_db() -> Iterator[sqlite3.Connection]:
    conn = db.connect(config.DB_PATH)
    db.init_db(conn)
    try:
        yield conn
    finally:
        conn.close()


def load_manifest(row: sqlite3.Row) -> Manifest:
    return Manifest.model_validate(json.loads(row["manifest_json"]))


def get_model_or_404(conn: sqlite3.Connection, model_id: str) -> sqlite3.Row:
    row = q.get_model(conn, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"unknown model_id {model_id!r}")
    return row
