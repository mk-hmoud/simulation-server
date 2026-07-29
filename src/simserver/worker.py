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
        manifest = json.loads(model_row["manifest_json"])
        self._geometry_params = {
            name for name, spec in manifest.get("parameters", {}).items() if spec.get("geometry")
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
            rebuild = self._needs_rebuild(job.params)
            self.backend.set_parameters(self._handle, job.params)
            if rebuild:
                self.backend.build_geometry(self._handle)
                self.backend.mesh(self._handle)
            self.backend.solve(self._handle, study=None)
            self._last_params = dict(job.params)

            for name, expression in job.outputs.items():
                value = self.backend.evaluate(self._handle, expression)
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
