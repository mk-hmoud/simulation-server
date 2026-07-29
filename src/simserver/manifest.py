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

from pydantic import BaseModel, Field

_VALUE_RE = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*(?:\[(.+)\])?\s*$")


class ParameterSpec(BaseModel):
    unit: str
    min: float
    max: float
    geometry: bool = False


class ModeSelection(BaseModel):
    strategy: str
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


class ManifestValidationError(ValueError):
    pass


def validate_params(manifest: Manifest, params: dict[str, str]) -> None:
    """Raise ManifestValidationError if any parameter is unknown or out of range.

    Values are matched against the manifest's declared unit by exact string
    match (e.g. "um" must be spelled "um", not converted from "mm") — there is
    no unit-conversion engine here, only a range check on the numeric magnitude.
    """
    for name, raw in params.items():
        spec = manifest.parameters.get(name)
        if spec is None:
            raise ManifestValidationError(f"unknown parameter {name!r} for model {manifest.model_id!r}")

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
