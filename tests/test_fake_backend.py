from __future__ import annotations

from pathlib import Path

import pytest

from simserver.backend import BackendError, ErrorClass, FakeBackend


@pytest.fixture
def model_path(tmp_path: Path) -> Path:
    p = tmp_path / "model.mph"
    p.write_bytes(b"not a real model, just needs to exist")
    return p


def test_load_missing_model_raises_validation_error(tmp_path: Path) -> None:
    backend = FakeBackend()
    with pytest.raises(BackendError) as exc_info:
        backend.load(tmp_path / "missing.mph")
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_happy_path_solve_evaluate_export(model_path: Path, tmp_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    backend.set_parameters(handle, {"lambda0": "1550[nm]"})
    backend.build_geometry(handle)
    backend.mesh(handle)
    backend.solve(handle, study="std1")

    value = backend.evaluate(handle, "real(ewfd.neff)")
    assert isinstance(value, float)

    # same expression + same parameters -> same synthetic value
    assert backend.evaluate(handle, "real(ewfd.neff)") == value

    export_path = tmp_path / "out" / "field.csv"
    backend.export(handle, "field_export_1", export_path)
    assert export_path.exists()

    backend.release(handle)


def test_mesh_before_geometry_raises_validation_error(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    with pytest.raises(BackendError) as exc_info:
        backend.mesh(handle)
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_evaluate_before_solve_raises_validation_error(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    backend.build_geometry(handle)
    backend.mesh(handle)
    with pytest.raises(BackendError) as exc_info:
        backend.evaluate(handle, "real(ewfd.neff)")
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_simulated_non_convergence_is_solver_class_error(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0, non_convergence_rate=1.0, seed=0)
    handle = backend.load(model_path)
    backend.build_geometry(handle)
    backend.mesh(handle)
    with pytest.raises(BackendError) as exc_info:
        backend.solve(handle, study=None)
    assert exc_info.value.error_class == ErrorClass.SOLVER


def test_different_parameters_change_evaluate_result(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    backend.build_geometry(handle)
    backend.mesh(handle)
    backend.set_parameters(handle, {"lambda0": "1550[nm]"})
    backend.solve(handle, study=None)
    v1 = backend.evaluate(handle, "real(ewfd.neff)")

    backend.set_parameters(handle, {"lambda0": "1600[nm]"})
    backend.solve(handle, study=None)
    v2 = backend.evaluate(handle, "real(ewfd.neff)")

    assert v1 != v2


def test_configure_sweep_makes_evaluate_return_one_value_per_point(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    backend.build_geometry(handle)
    backend.mesh(handle)
    backend.configure_sweep(handle, "lambda0", ["1550[nm]", "1560[nm]", "1570[nm]"])
    backend.solve(handle, study=None)

    values = backend.evaluate(handle, "real(ewfd.neff)")

    assert isinstance(values, list)
    assert len(values) == 3
    assert len(set(values)) == 3  # each sweep point varies the parameter, so all differ


def test_disable_sweep_reverts_evaluate_to_a_single_scalar(model_path: Path) -> None:
    backend = FakeBackend(solve_seconds=0, mesh_seconds=0)
    handle = backend.load(model_path)
    backend.build_geometry(handle)
    backend.mesh(handle)
    backend.configure_sweep(handle, "lambda0", ["1550[nm]", "1560[nm]"])
    backend.solve(handle, study=None)
    assert isinstance(backend.evaluate(handle, "real(ewfd.neff)"), list)

    backend.disable_sweep(handle)
    backend.solve(handle, study=None)

    assert isinstance(backend.evaluate(handle, "real(ewfd.neff)"), float)
