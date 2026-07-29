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

    def evaluate(self, handle: ModelHandle, expression: str) -> Any: ...

    def export(self, handle: ModelHandle, node: str, path: Path) -> None: ...

    def release(self, handle: ModelHandle) -> None: ...
