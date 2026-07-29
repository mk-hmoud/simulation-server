from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ModelHandle


class MphBackend:
    """Real SolverBackend, driving COMSOL through the `mph` library.

    NOT YET IMPLEMENTED. The plan is explicit that the mph API signatures below are
    approximate and must be confirmed against the installed version before writing
    logic on top of them (see tools/mph_explore.py, milestone M1). Run that script
    in the VM first, note any signature differences, then fill in the methods here.

    Expected shape, per the plan:
        client = mph.start(cores=C)
        model = client.load(path)
        model.parameter(name, '1550[nm]')   # value as string, units included
        model.build(); model.mesh(); model.solve(study=study)
        model.evaluate(expression, unit=None, dataset=None)
        model.export(node, path)
        client.remove(model)

    Also confirm empirically (M6, in the VM):
      - whether mph.start() launches an external COMSOL server subprocess or runs
        in-process — this determines whether a supervisor kill of the worker
        process needs a separate step to release the license, or whether killing
        the process tree is sufficient (see plan §5.3, §6 watchdog).
      - whether solve() raises distinguishable exceptions for non-convergence vs.
        license/infrastructure failures, so errors can be classified per §6
        without parsing message text.
    """

    def __init__(self, *, cores: int | None = None, version: str | None = None) -> None:
        self.cores = cores
        self.version = version
        self._client = None  # set on first use; one mph.start() per process lifetime

    def _ensure_client(self):
        if self._client is None:
            raise NotImplementedError(
                "MphBackend requires the real mph library and a COMSOL install; "
                "run tools/mph_explore.py in the VM first (M1) before implementing this."
            )
        return self._client

    def load(self, model_path: Path) -> ModelHandle:
        raise NotImplementedError

    def set_parameters(self, handle: ModelHandle, params: dict[str, str]) -> None:
        raise NotImplementedError

    def build_geometry(self, handle: ModelHandle) -> None:
        raise NotImplementedError

    def mesh(self, handle: ModelHandle) -> None:
        raise NotImplementedError

    def solve(self, handle: ModelHandle, study: str | None) -> None:
        raise NotImplementedError

    def evaluate(self, handle: ModelHandle, expression: str) -> Any:
        raise NotImplementedError

    def export(self, handle: ModelHandle, node: str, path: Path) -> None:
        raise NotImplementedError

    def release(self, handle: ModelHandle) -> None:
        raise NotImplementedError
