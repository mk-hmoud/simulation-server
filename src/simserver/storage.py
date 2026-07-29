"""On-disk model storage layout (plan §2): data/models/<model_id>/{model.mph,manifest.json}."""

from __future__ import annotations

import json
from pathlib import Path

from . import config
from .manifest import Manifest


def model_dir(model_id: str) -> Path:
    return config.MODELS_DIR / model_id


def save_model(model_id: str, mph_bytes: bytes, manifest: Manifest) -> Path:
    directory = model_dir(model_id)
    directory.mkdir(parents=True, exist_ok=True)
    model_path = directory / "model.mph"
    model_path.write_bytes(mph_bytes)
    (directory / "manifest.json").write_text(json.dumps(manifest.model_dump(), indent=2))
    return model_path
