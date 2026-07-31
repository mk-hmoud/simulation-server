"""Model manifest schema (plan §4) and job-parameter validation.

The manifest is the only thing that turns a client-submitted job into COMSOL
calls. Two things it must guarantee:
  - clients can never inject an arbitrary COMSOL expression — output names in
    a job resolve to expressions looked up from the manifest, never from the
    request body itself (see resolve_outputs below).
  - clients can never set a parameter the manifest doesn't declare, or outside
    its declared [min, max].
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, model_validator

_VALUE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?:\[(.+)\])?\s*$")


class ParameterSpec(BaseModel):
    unit: str
    min: float
    max: float
    geometry: bool = False


class ModeSelection(BaseModel):
    strategy: str
    # name of a manifest `outputs` entry whose expression returns one value
    # per eigenmode (e.g. "real(emw.neff)") — both strategies rank modes
    # against this array, never against a hardcoded physics-tag expression
    neff_output: str
    core_selection: str | None = None
    min_fraction: float | None = None
    neff_target_expression: str | None = None


class Manifest(BaseModel):
    model_id: str
    description: str = ""
    study: str | None = None
    parameters: dict[str, ParameterSpec]
    sweep_parameter: str | None = None
    mode_selection: ModeSelection | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    exports: list[str] = Field(default_factory=list)
    # per-model watchdog timeout (plan §6): how long a running job may hold a
    # worker before the supervisor kills the process tree and reclaims it
    timeout_seconds: float = 600.0

    @model_validator(mode="after")
    def _check_mode_selection(self) -> "Manifest":
        ms = self.mode_selection
        if ms is None:
            return self
        if ms.neff_output not in self.outputs:
            raise ValueError(f"mode_selection.neff_output {ms.neff_output!r} is not a declared output")
        if ms.strategy == "nearest_neff_to_target" and not ms.neff_target_expression:
            raise ValueError("mode_selection.strategy 'nearest_neff_to_target' requires neff_target_expression")
        if ms.strategy == "core_power_fraction" and not ms.core_selection:
            raise ValueError("mode_selection.strategy 'core_power_fraction' requires core_selection")
        return self


class ManifestValidationError(ValueError):
    pass


def parse_magnitude(raw: str) -> float:
    """Extract the bare numeric magnitude from a value string like '0.8[um]'
    (no unit conversion — just the number as written, for storage/reporting,
    e.g. results.sweep_value)."""
    match = _VALUE_RE.match(raw)
    if not match:
        raise ManifestValidationError(f"cannot parse value {raw!r}")
    return float(match.group(1))


def _validate_one_value(manifest: Manifest, name: str, spec: ParameterSpec, raw: str) -> None:
    match = _VALUE_RE.match(raw)
    if not match:
        raise ManifestValidationError(f"parameter {name!r}: cannot parse value {raw!r}")
    magnitude = float(match.group(1))
    unit = match.group(2)
    if unit is not None and unit != spec.unit:
        raise ManifestValidationError(
            f"parameter {name!r}: unit {unit!r} does not match manifest unit {spec.unit!r}"
        )
    if not (spec.min <= magnitude <= spec.max):
        raise ManifestValidationError(
            f"parameter {name!r}: value {magnitude} outside allowed range "
            f"[{spec.min}, {spec.max}] {spec.unit}"
        )


def validate_params(manifest: Manifest, params: dict[str, str | list[str]]) -> None:
    """Raise ManifestValidationError if any parameter is unknown or out of range.

    Values are matched against the manifest's declared unit by exact string
    match (e.g. "um" must be spelled "um", not converted from "mm") — there is
    no unit-conversion engine here, only a range check on the numeric magnitude.

    A list value (plan §7 "Sweeps": an in-COMSOL parametric sweep instead of N
    jobs) is only accepted for manifest.sweep_parameter; every other parameter
    must be a single value. Each element of a sweep list is validated the same
    way a scalar value would be.
    """
    for name, raw in params.items():
        spec = manifest.parameters.get(name)
        if spec is None:
            raise ManifestValidationError(f"unknown parameter {name!r} for model {manifest.model_id!r}")

        if isinstance(raw, list):
            if name != manifest.sweep_parameter:
                raise ManifestValidationError(
                    f"parameter {name!r}: a list of values is only allowed for the manifest's "
                    f"sweep_parameter ({manifest.sweep_parameter!r})"
                )
            if not raw:
                raise ManifestValidationError(f"parameter {name!r}: sweep value list must not be empty")
            for value in raw:
                _validate_one_value(manifest, name, spec, value)
        else:
            _validate_one_value(manifest, name, spec, raw)


def resolve_outputs(manifest: Manifest, requested: list[str] | None) -> dict[str, str]:
    """Map requested output names to their manifest expressions.

    The expression text always comes from the manifest, never from the caller
    — this is the boundary that keeps job submission from being able to smuggle
    arbitrary COMSOL expressions into the worker.
    """
    if requested is None:
        return dict(manifest.outputs)
    resolved: dict[str, str] = {}
    for name in requested:
        if name not in manifest.outputs:
            raise ManifestValidationError(f"unknown output {name!r} for model {manifest.model_id!r}")
        resolved[name] = manifest.outputs[name]
    return resolved
