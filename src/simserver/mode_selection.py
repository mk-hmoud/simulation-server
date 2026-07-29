"""Mode selection (plan §4): pick the fundamental core mode out of the many
eigenmodes a Wave Optics mode analysis returns — the plan is explicit that
the "right" mode is not at a stable index across parameter values, so this
cannot be skipped or hardcoded to e.g. index 0.

Only nearest_neff_to_target is implemented against real evaluate() calls.
core_power_fraction needs a domain integration operator that no checked-in
fixture has yet (fixtures/README.md "M1 findings" — spr_pcf_side_hole.mph's
model.selections() is empty) — implementing it blind, with no way to verify
against a real model, was an explicit decision to defer (not a decision to
implement half-blind and hope), so it raises clearly instead.
"""

from __future__ import annotations

from dataclasses import dataclass

from .backend.base import BackendError, ErrorClass
from .manifest import Manifest


@dataclass
class ModeSelectionResult:
    strategy: str
    selected_index: int
    n_modes_considered: int
    core_fraction: float | None = None


def _as_1d(value) -> list[float]:
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not isinstance(value, list):
        value = [value]
    return value


def select_mode(backend, handle, manifest: Manifest) -> ModeSelectionResult:
    mode_selection = manifest.mode_selection
    if mode_selection is None:
        raise BackendError("select_mode called but manifest has no mode_selection configured", ErrorClass.VALIDATION)

    if mode_selection.strategy == "nearest_neff_to_target":
        return _nearest_neff_to_target(backend, handle, manifest)
    if mode_selection.strategy == "core_power_fraction":
        raise BackendError(
            "mode_selection strategy 'core_power_fraction' is not implemented: it needs a domain "
            "integration operator over a core selection, which no checked-in fixture has yet "
            "(see fixtures/README.md 'M1 findings'). Use 'nearest_neff_to_target' until a fixture "
            "with a real core selection exists to implement and verify this against.",
            ErrorClass.VALIDATION,
        )
    raise BackendError(f"unknown mode_selection strategy: {mode_selection.strategy!r}", ErrorClass.VALIDATION)


def _nearest_neff_to_target(backend, handle, manifest: Manifest) -> ModeSelectionResult:
    mode_selection = manifest.mode_selection
    neff_expression = manifest.outputs[mode_selection.neff_output]
    neff_values = _as_1d(backend.evaluate(handle, neff_expression))
    target_values = _as_1d(backend.evaluate(handle, mode_selection.neff_target_expression))
    target = target_values[0]

    distances = [abs(value - target) for value in neff_values]
    selected_index = distances.index(min(distances))

    return ModeSelectionResult(
        strategy="nearest_neff_to_target",
        selected_index=selected_index,
        n_modes_considered=len(neff_values),
    )
