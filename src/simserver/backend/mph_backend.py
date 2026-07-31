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
    model.build(); model.mesh(); model.solve(study)  # study: str | Node — a plain str
                                                      # is a NAME lookup (GUI label, e.g.
                                                      # "Study 1"), NOT a tag lookup ("std1")
                                                      # — confirmed live via a LookupError
                                                      # when passing the tag directly
                                                      # (tools/mph_sweep_explore.py). This
                                                      # backend always resolves a tag to the
                                                      # actual Node first (_resolve_study).
    model.evaluate(expression) -> numpy.ndarray      # one value per eigenmode
                                                      # if solution is Eigenfrequency
    model.export(node, path)                         # node: str name under 'exports'
    client.remove(model)

Node names vs. tags: model.physics()/.studies()/etc. return GUI *labels*
("Electromagnetic Waves, Frequency Domain"), not the tags expressions use
("emw"). Never hardcode a physics prefix like "ewfd" — it varies per model.

Confirmed live in the VM (M6): mph.start() spawns comsolmphserver.exe as a
genuine OS-level child process of the Python worker (verified via
Get-CimInstance Win32_Process: the server's ParentProcessId matched the
worker's own pid exactly). The supervisor's existing psutil-based recursive
process-tree kill (supervisor.py) therefore already reaches and releases it
on a watchdog timeout — no separate release step needed.

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
  - what a real non-convergent solve actually raises, to check the "solver"
    default classification is right rather than a guess.
  - what a real license-checkout failure actually raises, to check the
    license-keyword heuristic actually matches real COMSOL wording.
  - core_power_fraction mode selection needs a domain selection to integrate
    over; the checked-in fixture has none (model.selections() == []), so this
    is unverified against a real model — see fixtures/README.md.

In-COMSOL sweeps (plan §7), confirmed live via tools/mph_sweep_explore.py:
  - a "Parametric Sweep" study extension is a feature node created directly
    on the study (Study.create('Parametric')), not nested under a separate
    container — its real property names are pname (list of parameter names)
    and plistarr (one value-list per parameter; COMSOL auto-normalizes
    unit-bearing strings like '0.8[um]' to base SI internally). plist (flat
    float array) exists too but wasn't the one that worked.
  - solving the study with the sweep feature enabled just works via the
    normal model.solve(study) call — no separate "run sweep" step.
  - the DEFAULT dataset ("Study 1//Solution 1") only ever reflects the LAST
    solved sweep point, not all of them — evaluating against it after a
    sweep silently gives you one point's data with no indication anything
    is missing. All points live in a second dataset COMSOL creates,
    "Study 1//Parametric Solutions 1", which evaluate(expr, dataset=...)
    returns as a flat array of length n_sweep_points * n_modes (outer-major:
    all modes for point 0, then point 1, etc.) — reshape (n_sweep, n_modes)
    to recover the 2D structure. Cross-checked against a real value: the
    middle third of an 18-length (3 points x 6 modes) result for l in
    [0.6, 0.8, 1.0][um] matched a standalone l=0.8[um] solve exactly.
  - passing outer=<int> against the default dataset was a dead end (only
    outer=1 didn't raise FlException('Invalid_property_value'), and it just
    returned the same last-point data as no outer= at all) — not used here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from .base import BackendError, ErrorClass

T = TypeVar("T")

_PARAMETRIC_SOLUTIONS_MARKER = "Parametric Solutions"

_LICENSE_KEYWORDS = ("license", "checkout", "flexlm", "feature", "seat")


def _classify(exc: Exception, *, default: ErrorClass) -> ErrorClass:
    if any(keyword in str(exc).lower() for keyword in _LICENSE_KEYWORDS):
        return ErrorClass.INFRASTRUCTURE
    return default


class MphModelHandle:
    __slots__ = ("model", "sweep_node", "sweep_active")

    def __init__(self, model: Any) -> None:
        self.model = model
        self.sweep_node = None  # cached "Parametric" feature Node, created lazily and reused
        self.sweep_active = False  # whether the sweep is enabled for the *next* solve


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
        except BackendError:
            raise  # already correctly classified (e.g. _resolve_study) — don't reclassify
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
        # study here is a TAG (e.g. manifest's "study": "std1"), but
        # Model.solve(str) treats a plain string as a NAME lookup, not a tag
        # lookup — passing the tag directly raises LookupError (confirmed live
        # via tools/mph_sweep_explore.py: "std1" is the tag, "Study 1" is the
        # name). Resolve to the actual Node by tag first.
        def _solve() -> None:
            target = self._resolve_study(handle.model, study) if study else None
            handle.model.solve(target)

        self._run(_solve, "solve", default=ErrorClass.SOLVER)

    @staticmethod
    def _resolve_study(model: Any, tag: str):
        for candidate in model / "studies":
            if candidate.tag() == tag:
                return candidate
        raise BackendError(f"no study with tag {tag!r}", ErrorClass.VALIDATION)

    @staticmethod
    def _only_study(model: Any):
        studies = list(model / "studies")
        if len(studies) != 1:
            raise BackendError(
                f"configure_sweep needs an explicit study tag when the model has {len(studies)} studies",
                ErrorClass.VALIDATION,
            )
        return studies[0]

    def _sweep_node(self, handle: MphModelHandle, study: str | None):
        if handle.sweep_node is None:
            target = self._resolve_study(handle.model, study) if study else self._only_study(handle.model)
            handle.sweep_node = target.create("Parametric")
        return handle.sweep_node

    def configure_sweep(
        self, handle: MphModelHandle, param_name: str, values: list[str], study: str | None = None
    ) -> None:
        def _configure() -> None:
            node = self._sweep_node(handle, study)
            node.property("pname", [param_name])
            node.property("plistarr", [list(values)])
            node.toggle("on")
            handle.sweep_active = True

        self._run(_configure, "configure_sweep", default=ErrorClass.VALIDATION)

    def disable_sweep(self, handle: MphModelHandle) -> None:
        def _disable() -> None:
            if handle.sweep_node is not None:
                handle.sweep_node.toggle("off")
            handle.sweep_active = False

        self._run(_disable, "disable_sweep", default=ErrorClass.SOLVER)

    @staticmethod
    def _sweep_dataset_name(model: Any) -> str:
        for name in model.datasets():
            if _PARAMETRIC_SOLUTIONS_MARKER in name:
                return name
        raise BackendError(
            "sweep is active but no 'Parametric Solutions' dataset was found after solving",
            ErrorClass.INFRASTRUCTURE,
        )

    def evaluate(self, handle: MphModelHandle, expression: str) -> Any:
        def _evaluate():
            if handle.sweep_active:
                dataset = self._sweep_dataset_name(handle.model)
                return handle.model.evaluate(expression, dataset=dataset)
            return handle.model.evaluate(expression)

        return self._run(_evaluate, "evaluate", default=ErrorClass.VALIDATION)

    def export(self, handle: MphModelHandle, node: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._run(lambda: handle.model.export(node, str(path)), "export", default=ErrorClass.VALIDATION)

    def release(self, handle: MphModelHandle) -> None:
        client = self._ensure_client()
        self._run(lambda: client.remove(handle.model), "release", default=ErrorClass.INFRASTRUCTURE)
