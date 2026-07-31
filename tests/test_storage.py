from __future__ import annotations

from pathlib import Path

import pytest

from simserver import config, storage
from simserver.manifest import Manifest


@pytest.fixture(autouse=True)
def models_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(config, "MODELS_DIR", tmp_path / "models")
    return tmp_path / "models"


def test_save_model_writes_mph_and_manifest(models_dir: Path) -> None:
    manifest = Manifest(model_id="m1", parameters={})
    path = storage.save_model("m1", b"fake mph bytes", manifest)

    assert path == models_dir / "m1" / "model.mph"
    assert path.read_bytes() == b"fake mph bytes"
    assert (models_dir / "m1" / "manifest.json").exists()


def test_delete_model_files_removes_the_directory(models_dir: Path) -> None:
    manifest = Manifest(model_id="m1", parameters={})
    storage.save_model("m1", b"stub", manifest)
    assert storage.model_dir("m1").exists()

    storage.delete_model_files("m1")

    assert not storage.model_dir("m1").exists()


def test_delete_model_files_is_a_noop_for_unknown_model(models_dir: Path) -> None:
    storage.delete_model_files("does-not-exist")  # must not raise
