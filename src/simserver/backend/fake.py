from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .base import BackendError, ErrorClass


@dataclass
class FakeModelHandle:
    model_path: Path
    parameters: dict[str, str] = field(default_factory=dict)
    geometry_built: bool = False
    meshed: bool = False
    solved: bool = False
    sweep: tuple[str, list[str]] | None = None  # (param_name, values) if configure_sweep() was called and not since disabled


class FakeBackend:
    """Synthetic SolverBackend so the queue/API/supervisor can be built and tested
    without consuming a COMSOL license. Real behaviour lives in MphBackend (M6)."""

    def __init__(
        self,
        *,
        solve_seconds: float = 0.1,
        mesh_seconds: float = 0.05,
        non_convergence_rate: float = 0.0,
        hang_rate: float = 0.0,
        hang_seconds: float = 3600.0,
        seed: int | None = None,
        n_modes: int = 1,
    ) -> None:
        self.solve_seconds = solve_seconds
        self.mesh_seconds = mesh_seconds
        self.non_convergence_rate = non_convergence_rate
        self.hang_rate = hang_rate
        self.hang_seconds = hang_seconds
        # n_modes > 1 makes evaluate() return a per-eigenmode array per sweep
        # point (outer-major under a sweep, matching the real mph backend), so
        # the worker's all-modes storage path can be tested without COMSOL.
        self.n_modes = n_modes
        self._rng = random.Random(seed)

    def load(self, model_path: Path) -> FakeModelHandle:
        if not model_path.exists():
            raise BackendError(f"model file not found: {model_path}", ErrorClass.VALIDATION)
        return FakeModelHandle(model_path=model_path)

    def set_parameters(self, handle: FakeModelHandle, params: dict[str, str]) -> None:
        handle.parameters.update(params)

    def build_geometry(self, handle: FakeModelHandle) -> None:
        handle.geometry_built = True
        handle.meshed = False

    def mesh(self, handle: FakeModelHandle) -> None:
        if not handle.geometry_built:
            raise BackendError("mesh() called before build_geometry()", ErrorClass.VALIDATION)
        time.sleep(self.mesh_seconds)
        handle.meshed = True

    def solve(self, handle: FakeModelHandle, study: str | None) -> None:
        if self._rng.random() < self.hang_rate:
            time.sleep(self.hang_seconds)
        time.sleep(self.solve_seconds)
        if self._rng.random() < self.non_convergence_rate:
            raise BackendError("simulated non-convergence", ErrorClass.SOLVER)
        handle.solved = True

    def configure_sweep(
        self, handle: FakeModelHandle, param_name: str, values: list[str], study: str | None = None
    ) -> None:
        handle.sweep = (param_name, list(values))

    def disable_sweep(self, handle: FakeModelHandle) -> None:
        handle.sweep = None

    def _synthetic_value(self, expression: str, parameters: dict[str, str]) -> float:
        digest = hash((expression, tuple(sorted(parameters.items()))))
        return 1.44 + (digest % 1_000_000) / 1e8

    def _point_values(self, expression: str, parameters: dict[str, str]):
        """One synthetic number per point (n_modes == 1, the historical shape),
        or a per-mode list when n_modes > 1."""
        base = self._synthetic_value(expression, parameters)
        if self.n_modes == 1:
            return base
        return [base + mode * 1e-3 for mode in range(self.n_modes)]

    def evaluate(self, handle: FakeModelHandle, expression: str) -> Any:
        if not handle.solved:
            raise BackendError("evaluate() called before solve()", ErrorClass.VALIDATION)
        # deterministic synthetic value derived from expression + current parameters,
        # so repeated evaluate() calls on the same state return the same number
        if handle.sweep is None:
            return self._point_values(expression, handle.parameters)
        # per sweep point, each varying with that point's value; under n_modes > 1
        # the per-mode arrays are concatenated outer-major (all modes for point 0,
        # then point 1, …) exactly as the real mph parametric-sweep dataset returns
        param_name, values = handle.sweep
        flat: list[float] = []
        for value in values:
            pv = self._point_values(expression, {**handle.parameters, param_name: value})
            if isinstance(pv, list):
                flat.extend(pv)
            else:
                flat.append(pv)
        return flat

    def export(self, handle: FakeModelHandle, node: str, path: Path) -> None:
        if not handle.solved:
            raise BackendError("export() called before solve()", ErrorClass.VALIDATION)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"# fake export of node={node!r} params={handle.parameters}\n")

    def release(self, handle: FakeModelHandle) -> None:
        handle.solved = False
