from __future__ import annotations

from pathlib import Path
from typing import Any

from .base import ModelHandle


class MphBackend:
    """Real SolverBackend, driving COMSOL through the `mph` library.

    NOT YET IMPLEMENTED. Fill in the methods here once M3+ need a real backend.

    Confirmed against mph 1.3.1 / COMSOL 6.2 via tools/mph_explore.py against
    fixtures/spr_pcf_side_hole.mph (see fixtures/README.md "M1 findings"):
        client = mph.start(cores=C)
        model = client.load(path)
        model.parameter(name, value)   # value as string incl. unit, e.g. '1.8[um]'
        model.build(); model.mesh(); model.solve()   # study arg untested so far
        model.evaluate(expression) -> numpy.ndarray   # one value per eigenmode
                                                       # if solution is Eigenfrequency
        client.remove(model)

    Node names vs. tags: model.physics()/.studies()/etc. return GUI *labels*
    ("Electromagnetic Waves, Frequency Domain"), not the tags expressions use
    ("emw"). Get tags via `Node.tag()` on `model/'physics'` children — never
    hardcode a physics prefix like "ewfd", it varies per model/version.

    Geometry-vs-non-geometry mesh skip (plan §4) is confirmed real: setting a
    non-geometry parameter left mesh() at ~0s, a geometry one pushed it to 0.4s
    on this coarse fixture — expect a much bigger gap on a real fine-mesh model.

    Still to confirm empirically (M6, in the VM):
      - model.solve(study=...) with an explicit study argument — only the
        no-arg form has been exercised so far.
      - whether mph.start() launches an external COMSOL server subprocess or runs
        in-process — this determines whether a supervisor kill of the worker
        process needs a separate step to release the license, or whether killing
        the process tree is sufficient (see plan §5.3, §6 watchdog).
      - whether solve() raises distinguishable exceptions for non-convergence vs.
        license/infrastructure failures, so errors can be classified per §6
        without parsing message text.
      - core_power_fraction mode selection needs a domain selection to integrate
        over; the checked-in fixture has none (model.selections() == []), so this
        is unverified against a real model — see fixtures/README.md.
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
