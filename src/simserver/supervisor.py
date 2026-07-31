"""Supervisor (plan §5.3/§5.4, §6 watchdog): spawn/monitor/recycle workers,
kill process trees on per-job timeout, and reconcile jobs orphaned by a dead
or killed worker back onto the queue (bounded retries, per plan §6's note
that a job which reliably kills its worker must not retry forever).

License concurrency was found to be effectively unlimited (RAM/CPU is the
real bottleneck), so unlike the plan's §5.4 sketch, pool sizing here is just
a fixed configured worker_count — no "probe one at a time and persist the
discovered ceiling" dance, since there's no ceiling to discover. What that
section's mechanism is still worth keeping: bring workers up one at a time
and fail loudly (raise) if one dies during its startup grace period, rather
than silently running with fewer workers than requested.
"""

from __future__ import annotations

import calendar
import json
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from . import db
from . import queue as q
from .manifest import Manifest


@dataclass
class SupervisorConfig:
    db_path: Path
    worker_count: int = 1
    backend: str = "fake"
    memory_threshold_mb: float | None = 2048.0
    default_watchdog_timeout_s: float = 600.0
    poll_interval_s: float = 2.0
    startup_grace_s: float = 15.0
    max_attempts: int = 2
    retry_backoff_s: float = 30.0


class WorkerStartupError(RuntimeError):
    """A worker died during its startup grace period (plan §5.4: fail loudly
    instead of silently bringing up fewer workers than requested)."""


def _parse_utc(timestamp: str) -> float:
    # jobs.started_at etc. are written by queue._now() via time.gmtime(), so
    # they must be parsed back as UTC (calendar.timegm), not local time
    return calendar.timegm(time.strptime(timestamp, "%Y-%m-%dT%H:%M:%S"))


class Supervisor:
    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.conn = db.connect(config.db_path)
        db.init_db(self.conn)
        self._procs: dict[str, subprocess.Popen] = {}
        self._manifest_cache: dict[str, Manifest] = {}

    def _worker_cmd(self, worker_id: str) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "simserver.cli",
            "--db",
            str(self.config.db_path),
            "worker",
            "--worker-id",
            worker_id,
            "--backend",
            self.config.backend,
        ]
        if self.config.memory_threshold_mb is not None:
            cmd += ["--memory-threshold-mb", str(self.config.memory_threshold_mb)]
        return cmd

    def _spawn(self, worker_id: str) -> subprocess.Popen:
        proc = subprocess.Popen(self._worker_cmd(worker_id))
        self._procs[worker_id] = proc
        return proc

    def start(self) -> None:
        for i in range(self.config.worker_count):
            worker_id = f"worker-{i + 1}"
            proc = self._spawn(worker_id)
            deadline = time.monotonic() + self.config.startup_grace_s
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise WorkerStartupError(
                        f"{worker_id} exited during startup (code {proc.returncode}); "
                        f"requested pool size {self.config.worker_count} not reached"
                    )
                time.sleep(min(0.2, self.config.startup_grace_s))

    def _manifest_for(self, model_id: str) -> Manifest | None:
        if model_id in self._manifest_cache:
            return self._manifest_cache[model_id]
        row = q.get_model(self.conn, model_id)
        if row is None:
            return None
        manifest = Manifest.model_validate(json.loads(row["manifest_json"]))
        self._manifest_cache[model_id] = manifest
        return manifest

    def _kill_process_tree(self, proc: subprocess.Popen, timeout: float = 5.0) -> None:
        """Recursive kill (plan §6: COMSOL spawns children that survive a plain
        kill). psutil is cross-platform, so the same code covers the Windows
        VM (where this matters for real) and Linux dev/test."""
        try:
            parent = psutil.Process(proc.pid)
        except psutil.NoSuchProcess:
            return
        procs = [parent, *parent.children(recursive=True)]
        for p in procs:
            try:
                p.terminate()
            except psutil.NoSuchProcess:
                pass
        _, alive = psutil.wait_procs(procs, timeout=timeout)
        for p in alive:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass

    def _reconcile_dead_worker(self, worker_id: str, *, reason: str) -> None:
        for job in q.list_running_jobs(self.conn):
            if job["worker_id"] != worker_id:
                continue
            result = q.requeue_or_fail(
                self.conn,
                job["id"],
                "infrastructure",
                reason,
                max_attempts=self.config.max_attempts,
                backoff_seconds=self.config.retry_backoff_s,
            )
            print(f"supervisor: job {job['id']} ({worker_id}): {reason} -> {result}", flush=True)

    def _poll_workers(self) -> None:
        for worker_id, proc in list(self._procs.items()):
            code = proc.poll()
            if code is None:
                continue
            del self._procs[worker_id]
            # a job left 'running' here means the worker died mid-job (crash,
            # not a clean recycle/drain exit, which only happens between jobs)
            self._reconcile_dead_worker(worker_id, reason=f"worker exited unexpectedly (code {code})")
            print(f"supervisor: {worker_id} exited (code {code})", flush=True)
        # respawning (if appropriate) happens uniformly in _top_up_pool(),
        # whether this exit was a crash, a memory recycle, or a maintenance
        # drain — it's the one place that knows whether to hold off

    def _check_watchdog(self) -> None:
        now = time.time()
        for job in q.list_running_jobs(self.conn):
            worker_id = job["worker_id"]
            proc = self._procs.get(worker_id)
            if proc is None:
                continue  # not one of ours, or already reconciled this tick
            manifest = self._manifest_for(job["model_id"])
            timeout_s = manifest.timeout_seconds if manifest else self.config.default_watchdog_timeout_s
            if now - _parse_utc(job["started_at"]) < timeout_s:
                continue

            print(f"supervisor: job {job['id']} on {worker_id} exceeded {timeout_s}s, killing worker", flush=True)
            self._kill_process_tree(proc)
            del self._procs[worker_id]
            q.requeue_or_fail(
                self.conn,
                job["id"],
                "infrastructure",
                f"watchdog timeout after {timeout_s}s",
                max_attempts=self.config.max_attempts,
                backoff_seconds=self.config.retry_backoff_s,
            )
            # respawn handled by _top_up_pool(), same as _poll_workers()

    def _top_up_pool(self) -> None:
        """Bring the pool back to its configured size after any exit (crash,
        memory recycle, or watchdog kill) — but never during maintenance mode
        (plan §7): draining means the pool should shrink to zero and stay
        there until resume, not get refilled the instant a worker exits."""
        if q.get_maintenance_mode(self.conn):
            return
        for i in range(self.config.worker_count):
            worker_id = f"worker-{i + 1}"
            if worker_id not in self._procs:
                print(f"supervisor: spawning {worker_id} (pool below target size)", flush=True)
                self._spawn(worker_id)

    def run_forever(self) -> None:
        self.start()
        stop = False

        def _handle_term(signum, frame) -> None:
            nonlocal stop
            stop = True

        # SIGINT covers Ctrl-C during dev; SIGTERM covers a real service stop
        # (plan §9: this runs under NSSM eventually) — without it, `kill`/a
        # service manager stop leaves worker child processes running orphaned
        previous_handler = signal.signal(signal.SIGTERM, _handle_term)
        try:
            while not stop:
                self._poll_workers()
                self._check_watchdog()
                self._top_up_pool()
                time.sleep(self.config.poll_interval_s)
        except KeyboardInterrupt:
            pass
        finally:
            signal.signal(signal.SIGTERM, previous_handler)
            self.shutdown()

    def shutdown(self) -> None:
        for proc in list(self._procs.values()):
            self._kill_process_tree(proc)
        self._procs.clear()
