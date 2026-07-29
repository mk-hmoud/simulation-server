from __future__ import annotations

import pytest

from simserver.backend.base import BackendError, ErrorClass
from simserver.manifest import Manifest, ModeSelection
from simserver.mode_selection import select_mode


class ExpressionBackend:
    """Minimal stand-in backend: evaluate() looks values up from a fixed
    table keyed by expression string, so mode_selection tests don't need a
    real solver or FakeBackend (which only ever returns single scalars)."""

    def __init__(self, table: dict[str, object]) -> None:
        self.table = table

    def evaluate(self, handle, expression: str):
        return self.table[expression]


def make_manifest(**mode_selection_kwargs) -> Manifest:
    return Manifest(
        model_id="m1",
        parameters={},
        outputs={"neff_real": "real(emw.neff)", "neff_imag": "imag(emw.neff)"},
        mode_selection=ModeSelection(neff_output="neff_real", **mode_selection_kwargs),
    )


def test_nearest_neff_to_target_picks_closest_mode() -> None:
    backend = ExpressionBackend(
        {
            "real(emw.neff)": [1.30, 1.44, 1.50],
            "n_silica": 1.45,
        }
    )
    manifest = make_manifest(strategy="nearest_neff_to_target", neff_target_expression="n_silica")

    result = select_mode(backend, handle=None, manifest=manifest)

    assert result.strategy == "nearest_neff_to_target"
    assert result.selected_index == 1  # 1.44 is closest to 1.45 (dist 0.01 vs 0.05/0.15)
    assert result.n_modes_considered == 3
    assert result.core_fraction is None


def test_nearest_neff_to_target_with_single_mode() -> None:
    backend = ExpressionBackend({"real(emw.neff)": [1.44], "n_silica": 1.30})
    manifest = make_manifest(strategy="nearest_neff_to_target", neff_target_expression="n_silica")

    result = select_mode(backend, handle=None, manifest=manifest)

    assert result.selected_index == 0
    assert result.n_modes_considered == 1


def test_core_power_fraction_raises_not_implemented() -> None:
    backend = ExpressionBackend({})
    manifest = make_manifest(strategy="core_power_fraction", core_selection="sel_core")

    with pytest.raises(BackendError) as exc_info:
        select_mode(backend, handle=None, manifest=manifest)
    assert exc_info.value.error_class == ErrorClass.VALIDATION
    assert "not implemented" in str(exc_info.value)


def test_select_mode_without_mode_selection_configured_raises() -> None:
    manifest = Manifest(model_id="m1", parameters={}, outputs={})
    with pytest.raises(BackendError) as exc_info:
        select_mode(ExpressionBackend({}), handle=None, manifest=manifest)
    assert exc_info.value.error_class == ErrorClass.VALIDATION


def test_manifest_rejects_neff_output_not_in_outputs() -> None:
    with pytest.raises(ValueError):
        Manifest(
            model_id="m1",
            parameters={},
            outputs={"other": "expr"},
            mode_selection=ModeSelection(
                neff_output="neff_real",
                strategy="nearest_neff_to_target",
                neff_target_expression="n_silica",
            ),
        )


def test_manifest_rejects_nearest_strategy_without_target_expression() -> None:
    with pytest.raises(ValueError):
        Manifest(
            model_id="m1",
            parameters={},
            outputs={"neff_real": "real(emw.neff)"},
            mode_selection=ModeSelection(neff_output="neff_real", strategy="nearest_neff_to_target"),
        )
