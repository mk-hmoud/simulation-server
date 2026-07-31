"""Shared data-layout paths (plan §2) and API auth keys (plan §9),
overridable via env var for tests.
"""

from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("SIMSERVER_DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("SIMSERVER_DB_PATH", str(DATA_ROOT / "jobs.db")))
MODELS_DIR = DATA_ROOT / "models"
RESULTS_DIR = DATA_ROOT / "results"

# plan §9: model upload is a file-write endpoint, restricted to an admin key
# separate from the job-submission key. Unset means that tier's auth is
# disabled (dev mode) — production deployments must set both explicitly.
JOB_API_KEY = os.environ.get("SIMSERVER_JOB_API_KEY") or None
ADMIN_API_KEY = os.environ.get("SIMSERVER_ADMIN_API_KEY") or None
