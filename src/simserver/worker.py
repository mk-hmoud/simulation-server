"""Single-worker claim/execute loop (plan §5.1), model-affinity aware (§5.2).

Job submissions are validated against a model's manifest at the API layer
(M4) before they ever become a job row; the worker trusts params/outputs as
already-resolved and only reads the manifest here for the geometry-flag set
used to decide whether a rebuild is needed, and for sweep/mode-selection
configuration.

Sweeps (plan §7): a list value on the manifest's sweep_parameter means "run
this as one in-COMSOL parametric sweep", not N jobs. The backend's evaluate()
then returns every sweep point's data in one flat array (confirmed live via
tools/mph_sweep_explore.py — see mph_backend.py), fetched once per output
expression and sliced per point here, rather than calling evaluate() once per
point (which would be both wasteful and, for a mode-dependent expression,
wrong — it's a single Java-side call regardless of how many points there are).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil

from . import queue as q
from .backend.base import BackendError, ErrorClass, ModelHandle, SolverBackend
from .manifest import Manifest, parse_magnitude
from .mode_selection import check_strategy_supported, nearest_index


def _to_flat_list(raw) -> list:
    if hasattr(raw, "tolist"):
        raw = raw.tolist()
    if not isinstance(raw, list):
        raw = [raw]
    return raw


def _slice_for_point(raw, sweep_index: int, n_points: int) -> list:
    """Split a possibly-flattened (n_points * k) evaluate() result into the
    slice belonging to one sweep point. Degenerates to "the whole thing" when
    n_points == 1 (the non-swept case), so callers don't need to special-case
    it — this also transparently handles either shape a non-mode-dependent
    expression might come back as under a sweep (length n_points, or length
    n_points * n_modes with the same value repeated per mode): unverified
    against a real model either way, but this slicing is correct regardless
    of which one it turns out to be.
    """
    flat = _to_flat_list(raw)
    total = len(flat)
    if total % n_points != 0:
        raise BackendError(
            f"sweep result has {total} values, not evenly divisible by {n_points} sweep points",
            ErrorClass.INFRASTRUCTURE,
        )
    per_point = total // n_points
    start = sweep_index * per_point
    return flat[start : start + per_point]


def _resolve_point_value(point_values: list, selected_index: int | None):
    if len(point_values) == 1:
        return point_values[0]
    if selected_index is None:
        raise BackendError(
            f"expression returned {len(point_values)} values (per-mode array) but no mode_selection "
            "is configured in the manifest to pick one",
            ErrorClass.VALIDATION,
        )
    return point_values[selected_index]


class Worker:
    def __init__(
        self,
        conn,
        backend: SolverBackend,
        worker_id: str,
        *,
        poll_interval: float = 0.5,
        memory_threshold_bytes: int | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.worker_id = worker_id
        self.poll_interval = poll_interval
        self.memory_threshold_bytes = memory_threshold_bytes
        self._loaded_model_id: str | None = None
        self._handle: ModelHandle | None = None
        self._last_params: dict[str, str] = {}
        self._geometry_params: set[str] = set()
        self._manifest: Manifest | None = None
        self._process = psutil.Process(os.getpid())

    def _load_model_if_needed(self, model_id: str) -> None:
        if self._loaded_model_id == model_id:
            return
        if self._handle is not None:
            self.backend.release(self._handle)
            self._handle = None
        model_row = q.get_model(self.conn, model_id)
        if model_row is None:
            raise BackendError(f"unknown model_id: {model_id!r}", ErrorClass.VALIDATION)
        self._manifest = Manifest.model_validate(json.loads(model_row["manifest_json"]))
        self._geometry_params = {
            name for name, spec in self._manifest.parameters.items() if spec.geometry
        }
        self._handle = self.backend.load(Path(model_row["path"]))
        self._loaded_model_id = model_id
        self._last_params = {}

    def _needs_rebuild(self, params: dict[str, str]) -> bool:
        return any(params.get(name) != self._last_params.get(name) for name in self._geometry_params)

    def process_one(self) -> bool:
        """Claim and run a single job. Returns False if the queue was empty."""
        job = q.claim_next_job(self.conn, self.worker_id, preferred_model_id=self._loaded_model_id)
        if job is None:
            return False

        try:
            self._load_model_if_needed(job.model_id)

            sweep_param = self._manifest.sweep_parameter
            raw_sweep_values = job.params.get(sweep_param) if sweep_param else None
            is_sweep = isinstance(raw_sweep_values, list)

            params_for_set = dict(job.params)
            if is_sweep:
                del params_for_set[sweep_param]
                self.backend.configure_sweep(
                    self._handle, sweep_param, raw_sweep_values, study=self._manifest.study
                )
            else:
                self.backend.disable_sweep(self._handle)

            rebuild = self._needs_rebuild(params_for_set)
            self.backend.set_parameters(self._handle, params_for_set)
            if rebuild:
                t0 = time.monotonic()
                self.backend.build_geometry(self._handle)
                self.backend.mesh(self._handle)
                print(f"worker {self.worker_id}: job {job.id}: rebuilt geometry+mesh ({time.monotonic() - t0:.1f}s)", flush=True)
            else:
                print(f"worker {self.worker_id}: job {job.id}: skipped rebuild (no geometry parameter changed)", flush=True)

            t0 = time.monotonic()
            self.backend.solve(self._handle, study=self._manifest.study)
            print(f"worker {self.worker_id}: job {job.id}: solved ({time.monotonic() - t0:.1f}s)", flush=True)
            self._last_params = dict(params_for_set)

            n_points = len(raw_sweep_values) if is_sweep else 1

            # fetch each output's raw evaluate() result once (not once per
            # sweep point — see module docstring), then slice per point below
            raw_outputs = {name: self.backend.evaluate(self._handle, expr) for name, expr in job.outputs.items()}

            mode_selection = self._manifest.mode_selection
            raw_neff = raw_target = None
            if mode_selection is not None:
                check_strategy_supported(mode_selection)
                neff_expr = self._manifest.outputs[mode_selection.neff_output]
                raw_neff = self.backend.evaluate(self._handle, neff_expr)
                raw_target = self.backend.evaluate(self._handle, mode_selection.neff_target_expression)

            for sweep_index in range(n_points):
                sweep_value = parse_magnitude(raw_sweep_values[sweep_index]) if is_sweep else None

                selected_index: int | None = None
                if mode_selection is not None:
                    neff_point = _slice_for_point(raw_neff, sweep_index, n_points)
                    target_point = _slice_for_point(raw_target, sweep_index, n_points)
                    selected_index = nearest_index(neff_point, target_point[0])
                    q.write_mode_selection(
                        self.conn,
                        job.id,
                        mode_selection.strategy,
                        len(neff_point),
                        sweep_index=sweep_index,
                        core_fraction=None,
                    )

                for name, raw in raw_outputs.items():
                    point_values = _slice_for_point(raw, sweep_index, n_points)
                    value = _resolve_point_value(point_values, selected_index)
                    q.write_result(self.conn, job.id, name, value, sweep_index=sweep_index, sweep_value=sweep_value)

            q.mark_done(self.conn, job.id)
        except BackendError as exc:
            q.mark_failed(self.conn, job.id, exc.error_class.value, str(exc))
        except Exception as exc:  # noqa: BLE001 - unclassified failure, treat as infrastructure per plan §6
            q.mark_failed(self.conn, job.id, ErrorClass.INFRASTRUCTURE.value, repr(exc))
        return True

    def _should_recycle(self) -> bool:
        """Recycle on memory threshold, not job count (plan §5.3): COMSOL's Java
        heap grows across repeated loads/solves, and every restart costs a
        license release + re-checkout + startup time, which is unhidden dead
        time with few seats — so only recycle when RSS actually demands it."""
        if self.memory_threshold_bytes is None:
            return False
        return self._process.memory_info().rss >= self.memory_threshold_bytes

    def run_forever(self) -> None:
        while True:
            if not self.process_one():
                time.sleep(self.poll_interval)
                continue
            if self._should_recycle():
                print(
                    f"worker {self.worker_id}: RSS over threshold, exiting for recycle",
                    flush=True,
                )
                return
