"""Real SolverBackend, driving COMSOL through the `mph` library.

Confirmed against mph 1.3.1 / COMSOL 6.2, partly via tools/mph_explore.py
against fixtures/spr_pcf_side_hole.mph (fixtures/README.md "M1 findings"),
partly by reading the installed mph package's own source directly:
    client = mph.start(cores=C, version=V)   # only the FIRST call in a process
                                              # matters — mph caches and returns
                                              # the same Client on later calls
                                              # (hard constraint: one JVM/process)
    model = client.load(path)
    model.parameter(name, value)             # value as string incl. unit, e.g. '1.8[um]'
    model.build(); model.mesh(); model.solve(study)  # study: str | None, confirmed
                                                      # from mph.model.Model.solve's
                                                      # own signature/source
    model.evaluate(expression) -> numpy.ndarray      # one value per eigenmode
                                                      # if solution is Eigenfrequency
    model.export(node, path)                         # node: str name under 'exports'
    client.remove(model)

Node names vs. tags: model.physics()/.studies()/etc. return GUI *labels*
("Electromagnetic Waves, Frequency Domain"), not the tags expressions use
("emw"). Never hardcode a physics prefix like "ewfd" — it varies per model.

Error classification (plan §6) is a best-effort heuristic, not yet verified
against a real failure: mph doesn't define its own exception types for
COMSOL/Java-layer errors, it lets whatever JPype wrapped the Java exception
as bubble straight up, so a raised exception's message is all we have to work
with. A message mentioning license/checkout/feature/seat is classified as
`infrastructure`; anything else raised from build/mesh/solve is classified as
`solver` (non-convergence, singular matrix, meshing failure); anything else
from evaluate/export/load is classified as `validation` (a bad expression/
node/path is a manifest or config bug, not a numerical solver outcome).
**This has not been exercised against a real non-convergent solve or a real
license-checkout failure yet** — the classification logic is where to look
first if failures come back misclassified in production.

Still to confirm empirically (in the VM, ideally by deliberately provoking
each failure mode once):
  - whether mph.start() launches an external COMSOL server subprocess or runs
    in-process — this determines whether a supervisor kill of the worker
    process needs a separate step to release the license, or whether killing
    the process tree is sufficient (see plan §5.3, §6 watchdog).
  - what a real non-convergent solve actually raises, to check the "solver"
    default classification is right rather than a guess.
  - what a real license-checkout failure actually raises, to check the
    license-keyword heuristic actually matches real COMSOL wording.
  - core_power_fraction mode selection needs a domain selection to integrate
    over; the checked-in fixture has none (model.selections() == []), so this
    is unverified against a real model — see fixtures/README.md.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from .base import BackendError, ErrorClass

T = TypeVar("T")

_LICENSE_KEYWORDS = ("license", "checkout", "flexlm", "feature", "seat")


def _classify(exc: Exception, *, default: ErrorClass) -> ErrorClass:
    if any(keyword in str(exc).lower() for keyword in _LICENSE_KEYWORDS):
        return ErrorClass.INFRASTRUCTURE
    return default


class MphModelHandle:
    __slots__ = ("model",)

    def __init__(self, model: Any) -> None:
        self.model = model


class MphBackend:
    def __init__(self, *, cores: int | None = None, version: str | None = None) -> None:
        self.cores = cores
        self.version = version
        self._client = None  # mph.start() called at most once per process lifetime

    def _ensure_client(self):
        if self._client is None:
            import mph  # deferred: only needed where mph + COMSOL are installed

            self._client = mph.start(cores=self.cores, version=self.version)
        return self._client

    def _run(self, fn: Callable[[], T], phase: str, *, default: ErrorClass) -> T:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - reclassified below, not swallowed
            raise BackendError(f"{phase} failed: {exc}", _classify(exc, default=default)) from exc

    def load(self, model_path: Path) -> MphModelHandle:
        client = self._ensure_client()
        model = self._run(lambda: client.load(str(model_path)), "load", default=ErrorClass.VALIDATION)
        return MphModelHandle(model)

    def set_parameters(self, handle: MphModelHandle, params: dict[str, str]) -> None:
        def _set() -> None:
            for name, value in params.items():
                handle.model.parameter(name, value)

        self._run(_set, "set_parameters", default=ErrorClass.VALIDATION)

    def build_geometry(self, handle: MphModelHandle) -> None:
        self._run(handle.model.build, "build_geometry", default=ErrorClass.SOLVER)

    def mesh(self, handle: MphModelHandle) -> None:
        self._run(handle.model.mesh, "mesh", default=ErrorClass.SOLVER)

    def solve(self, handle: MphModelHandle, study: str | None) -> None:
        self._run(lambda: handle.model.solve(study), "solve", default=ErrorClass.SOLVER)

    def evaluate(self, handle: MphModelHandle, expression: str) -> Any:
        return self._run(lambda: handle.model.evaluate(expression), "evaluate", default=ErrorClass.VALIDATION)

    def export(self, handle: MphModelHandle, node: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(lambda: handle.model.export(node, str(path)), "export", default=ErrorClass.VALIDATION)

    def release(self, handle: MphModelHandle) -> None:
        client = self._ensure_client()
        self._run(lambda: client.remove(handle.model), "release", default=ErrorClass.INFRASTRUCTURE)
