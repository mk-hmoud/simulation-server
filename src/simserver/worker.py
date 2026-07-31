"""Single-worker claim/execute loop (plan §5.1), model-affinity aware (§5.2).

Job submissions are validated against a model's manifest at the API layer
(M4) before they ever become a job row; the worker trusts params/outputs as
already-resolved and only reads the manifest here for the geometry-flag set
used to decide whether a rebuild is needed.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import psutil

from . import queue as q
from .backend.base import BackendError, ErrorClass, ModelHandle, SolverBackend
from .manifest import Manifest
from .mode_selection import select_mode


def _scalar_size(value) -> int:
    size = getattr(value, "size", None)
    if size is not None:
        return int(size)
    if isinstance(value, (list, tuple)):
        return len(value)
    return 1


def _to_native(value):
    return value.item() if hasattr(value, "item") else value


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

    def _resolve_output_value(self, raw, selected_index: int | None):
        size = _scalar_size(raw)
        if size == 1:
            return _to_native(raw)
        if selected_index is None:
            raise BackendError(
                f"expression returned {size} values (per-mode array) but no mode_selection "
                "is configured in the manifest to pick one",
                ErrorClass.VALIDATION,
            )
        return _to_native(raw[selected_index])

    def process_one(self) -> bool:
        """Claim and run a single job. Returns False if the queue was empty."""
        job = q.claim_next_job(self.conn, self.worker_id, preferred_model_id=self._loaded_model_id)
        if job is None:
            return False

        try:
            self._load_model_if_needed(job.model_id)
            rebuild = self._needs_rebuild(job.params)
            self.backend.set_parameters(self._handle, job.params)
            if rebuild:
                t0 = time.monotonic()
                self.backend.build_geometry(self._handle)
                self.backend.mesh(self._handle)
                print(f"worker {self.worker_id}: job {job.id}: rebuilt geometry+mesh ({time.monotonic() - t0:.1f}s)", flush=True)
            else:
                print(f"worker {self.worker_id}: job {job.id}: skipped rebuild (no geometry parameter changed)", flush=True)
            t0 = time.monotonic()
            self.backend.solve(self._handle, study=None)
            print(f"worker {self.worker_id}: job {job.id}: solved ({time.monotonic() - t0:.1f}s)", flush=True)
            self._last_params = dict(job.params)

            selected_index: int | None = None
            if self._manifest.mode_selection is not None:
                mode_result = select_mode(self.backend, self._handle, self._manifest)
                selected_index = mode_result.selected_index
                q.write_mode_selection(
                    self.conn,
                    job.id,
                    mode_result.strategy,
                    mode_result.n_modes_considered,
                    core_fraction=mode_result.core_fraction,
                )

            for name, expression in job.outputs.items():
                raw = self.backend.evaluate(self._handle, expression)
                value = self._resolve_output_value(raw, selected_index)
                q.write_result(self.conn, job.id, name, value)
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
