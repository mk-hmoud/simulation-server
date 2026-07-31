from __future__ import annotations

from pathlib import Path

import pytest

from simserver.backend.base import BackendError, ErrorClass
from simserver.backend.mph_backend import MphBackend, MphModelHandle, _classify


class FakeSweepNode:
    def __init__(self) -> None:
        self._properties: dict[str, object] = {}
        self.toggled: list[str] = []

    def property(self, name: str, value=None):
        if value is None:
            return self._properties.get(name)
        self._properties[name] = value
        return None

    def toggle(self, action: str) -> None:
        self.toggled.append(action)


class FakeStudyNode:
    def __init__(self, tag: str) -> None:
        self._tag = tag
        self.created: list[str] = []

    def tag(self) -> str:
        return self._tag

    def create(self, type_name: str) -> FakeSweepNode:
        self.created.append(type_name)
        return FakeSweepNode()


class FakeMphModel:
    def __init__(self, study_tags: tuple[str, ...] = ("std1",)) -> None:
        self.params: dict[str, str] = {}
        self.solved_with: object = "not called"
        self.exported: tuple[str, str] | None = None
        self._solve_exc: Exception | None = None
        self._studies = [FakeStudyNode(tag) for tag in study_tags]
        # simulates COMSOL creating "Study 1//Parametric Solutions 1" only
        # after a sweep has actually been solved (tools/mph_sweep_explore.py
        # findings) — tests can flip this to prove evaluate() looks for it
        self._sweep_dataset_exists = False
        self.last_evaluate_dataset: str | None = None

    def __truediv__(self, other: str):
        if other == "studies":
            return self._studies
        raise KeyError(other)

    def parameter(self, name: str, value: str) -> None:
        self.params[name] = value

    def build(self) -> None:
        pass

    def mesh(self) -> None:
        pass

    def solve(self, study) -> None:
        if self._solve_exc:
            raise self._solve_exc
        self.solved_with = study

    def datasets(self) -> list[str]:
        names = ["Study 1//Solution 1"]
        if self._sweep_dataset_exists:
            names.append("Study 1//Parametric Solutions 1")
        return names

    def evaluate(self, expression: str, dataset: str | None = None):
        self.last_evaluate_dataset = dataset
        if expression == "bad":
            raise RuntimeError("Undefined variable: bad")
        if dataset is not None:
            return [1.1, 2.2, 3.3]  # stand-in flattened sweep result
        return 1.44

    def export(self, node: str, path: str) -> None:
        self.exported = (node, path)


class FakeClient:
    def __init__(self, model: FakeMphModel) -> None:
        self.model = model
        self.removed: list[object] = []

    def load(self, path: str) -> FakeMphModel:
        return self.model

    def remove(self, model: object) -> None:
        self.removed.append(model)


def make_backend(model: FakeMphModel | None = None) -> tuple[MphBackend, FakeMphModel]:
    model = model or FakeMphModel()
    backend = MphBackend()
    backend._client = FakeClient(model)  # bypass mph.start(); no COMSOL needed for these tests
    return backend, model


def test_classify_license_keyword_is_infrastructure() -> None:
    exc = Exception("No license available for feature WAVEOPTICS")
    assert _classify(exc, default=ErrorClass.SOLVER) == ErrorClass.INFRASTRUCTURE


def test_classify_falls_back_to_default_when_no_license_keyword() -> None:
    exc = Exception("mesh failed: singular matrix")
    assert _classify(exc, default=ErrorClass.SOLVER) == ErrorClass.SOLVER


def test_load_wraps_model_in_handle(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    assert isinstance(handle, MphModelHandle)
    assert handle.model is model


def test_set_parameters_calls_model_parameter_per_entry(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    backend.set_parameters(handle, {"lambda0": "1550[nm]", "na": "1.33"})
    assert model.params == {"lambda0": "1550[nm]", "na": "1.33"}


def test_solve_resolves_study_tag_to_node_not_raw_string(tmp_path: Path) -> None:
    # model.solve(str) does a NAME lookup, not a tag lookup (confirmed live via
    # tools/mph_sweep_explore.py: "std1" is a tag, the name is "Study 1", and
    # passing the tag string directly raised LookupError) — so the backend
    # must resolve the tag to the actual study Node before calling solve()
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    backend.solve(handle, "std1")
    assert isinstance(model.solved_with, FakeStudyNode)
    assert model.solved_with.tag() == "std1"


def test_solve_with_no_study_passes_none(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    backend.solve(handle, None)
    assert model.solved_with is None


def test_solve_with_unknown_study_tag_is_validation_error_not_reclassified(tmp_path: Path) -> None:
    backend, _ = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    with pytest.raises(BackendError) as exc_info:
        backend.solve(handle, "no-such-tag")
    # must stay VALIDATION, not get swept into the generic SOLVER default by
    # _run's broad except — this was a real bug caught while adding this test
    assert exc_info.value.error_class == ErrorClass.VALIDATION
    assert "no-such-tag" in str(exc_info.value)


def test_solve_failure_without_license_keyword_is_solver_class(tmp_path: Path) -> None:
    model = FakeMphModel()
    model._solve_exc = RuntimeError("solution did not converge")
    backend, _ = make_backend(model)
    handle = backend.load(tmp_path / "m.mph")

    with pytest.raises(BackendError) as exc_info:
        backend.solve(handle, None)
    assert exc_info.value.error_class == ErrorClass.SOLVER


def test_solve_failure_with_license_keyword_is_infrastructure_class(tmp_path: Path) -> None:
    model = FakeMphModel()
    model._solve_exc = RuntimeError("Could not check out license for feature WAVEOPTICS")
    backend, _ = make_backend(model)
    handle = backend.load(tmp_path / "m.mph")

    with pytest.raises(BackendError) as exc_info:
        backend.solve(handle, None)
    assert exc_info.value.error_class == ErrorClass.INFRASTRUCTURE


def test_evaluate_returns_value(tmp_path: Path) -> None:
    backend, _ = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    assert backend.evaluate(handle, "real(emw.neff)") == 1.44


def test_evaluate_failure_is_validation_class(tmp_path: Path) -> None:
    backend, _ = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    with pytest.raises(BackendError) as exc_info:
        backend.evaluate(handle, "bad")
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_export_creates_parent_dir_and_calls_model_export(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    out = tmp_path / "out" / "field.csv"

    backend.export(handle, "field_export_1", out)

    assert model.exported == ("field_export_1", str(out))
    assert out.parent.exists()


def test_release_calls_client_remove_with_the_model(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    backend.release(handle)

    assert backend._client.removed == [model]


def test_configure_sweep_creates_parametric_node_and_sets_properties(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    backend.configure_sweep(handle, "l", ["0.6[um]", "0.8[um]", "1.0[um]"], study="std1")

    study = model._studies[0]
    assert study.created == ["Parametric"]
    node = handle.sweep_node
    assert node.property("pname") == ["l"]
    assert node.property("plistarr") == [["0.6[um]", "0.8[um]", "1.0[um]"]]
    assert node.toggled == ["on"]


def test_configure_sweep_reuses_cached_node_across_calls(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    backend.configure_sweep(handle, "l", ["0.6[um]"], study="std1")
    first_node = handle.sweep_node
    backend.configure_sweep(handle, "l", ["0.9[um]"], study="std1")

    assert handle.sweep_node is first_node
    assert model._studies[0].created == ["Parametric"]  # only created once


def test_configure_sweep_with_no_study_arg_uses_the_only_study(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    backend.configure_sweep(handle, "l", ["0.6[um]"])  # no study given

    assert model._studies[0].created == ["Parametric"]


def test_configure_sweep_with_multiple_studies_requires_explicit_study(tmp_path: Path) -> None:
    model = FakeMphModel(study_tags=("std1", "std2"))
    backend, _ = make_backend(model)
    handle = backend.load(tmp_path / "m.mph")

    with pytest.raises(BackendError) as exc_info:
        backend.configure_sweep(handle, "l", ["0.6[um]"])
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_disable_sweep_toggles_node_off_and_is_a_noop_if_never_configured(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    backend.disable_sweep(handle)  # no sweep configured yet — must not raise

    backend.configure_sweep(handle, "l", ["0.6[um]"], study="std1")
    backend.disable_sweep(handle)

    assert handle.sweep_node.toggled == ["on", "off"]
    assert handle.sweep_active is False


def test_evaluate_uses_default_dataset_when_no_sweep_active(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")

    result = backend.evaluate(handle, "real(emw.neff)")

    assert result == 1.44
    assert model.last_evaluate_dataset is None


def test_evaluate_uses_parametric_solutions_dataset_when_sweep_active(tmp_path: Path) -> None:
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    backend.configure_sweep(handle, "l", ["0.6[um]", "0.8[um]", "1.0[um]"], study="std1")
    model._sweep_dataset_exists = True  # simulates COMSOL having actually solved the sweep

    result = backend.evaluate(handle, "real(emw.neff)")

    assert result == [1.1, 2.2, 3.3]
    assert model.last_evaluate_dataset == "Study 1//Parametric Solutions 1"


def test_evaluate_with_sweep_active_but_no_dataset_yet_is_infrastructure_error(tmp_path: Path) -> None:
    # sweep configured but not actually solved yet (or solve failed) — the
    # "Parametric Solutions" dataset COMSOL creates on solve doesn't exist
    backend, model = make_backend()
    handle = backend.load(tmp_path / "m.mph")
    backend.configure_sweep(handle, "l", ["0.6[um]"], study="std1")

    with pytest.raises(BackendError) as exc_info:
        backend.evaluate(handle, "real(emw.neff)")
    assert exc_info.value.error_class == ErrorClass.INFRASTRUCTURE
