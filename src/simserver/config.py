"""Shared data-layout paths (plan §2), overridable via env var for tests."""

from __future__ import annotations

import os
from pathlib import Path

DATA_ROOT = Path(os.environ.get("SIMSERVER_DATA_ROOT", "data"))
DB_PATH = Path(os.environ.get("SIMSERVER_DB_PATH", str(DATA_ROOT / "jobs.db")))
MODELS_DIR = DATA_ROOT / "models"
RESULTS_DIR = DATA_ROOT / "results"
