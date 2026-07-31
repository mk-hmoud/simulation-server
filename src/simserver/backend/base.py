from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class ErrorClass(str, Enum):
    """Retry policy differs per class — see plan §6. Never merge these."""

    INFRASTRUCTURE = "infrastructure"  # license checkout, worker killed, VM hiccup — retry with backoff
    SOLVER = "solver"  # non-convergence, singular matrix, meshing failure — never retry
    VALIDATION = "validation"  # bad params / unknown model — never retry, should be rejected earlier


class BackendError(Exception):
    def __init__(self, message: str, error_class: ErrorClass) -> None:
        super().__init__(message)
        self.error_class = error_class


class ModelHandle(Protocol):
    """Opaque per-backend handle returned by load(). Backends define their own concrete type."""


class SolverBackend(Protocol):
    def load(self, model_path: Path) -> ModelHandle: ...

    def set_parameters(self, handle: ModelHandle, params: dict[str, str]) -> None: ...

    def build_geometry(self, handle: ModelHandle) -> None: ...

    def mesh(self, handle: ModelHandle) -> None: ...

    def solve(self, handle: ModelHandle, study: str | None) -> None: ...

    def configure_sweep(
        self, handle: ModelHandle, param_name: str, values: list[str], study: str | None = None
    ) -> None:
        """Set up an in-COMSOL parametric sweep over one parameter (plan §7
        "Sweeps"), attached to `study` (a tag) or the model's only study if
        there's exactly one and `study` is omitted. Idempotent: safe to call
        again with different values on an already-swept handle. Must be
        paired with disable_sweep() before a subsequent non-swept job reuses
        the same loaded model — the swept state otherwise persists across
        solves."""
        ...

    def disable_sweep(self, handle: ModelHandle) -> None:
        """Turn off a previously configured sweep (no-op if none is active),
        so the next solve() on this handle is a plain single-point solve."""
        ...

    def evaluate(self, handle: ModelHandle, expression: str) -> Any: ...

    def export(self, handle: ModelHandle, node: str, path: Path) -> None: ...

    def release(self, handle: ModelHandle) -> None: ...
