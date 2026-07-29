# simulation-server

HTTP job API for running COMSOL simulations (SPR-PCF mode analysis) via `mph`,
inside a Windows VM. Full design in the plan doc (kept outside this repo).

## Status

Built without a COMSOL license / VM access:

- `src/simserver/backend/` — `SolverBackend` protocol, `FakeBackend` (synthetic,
  for dev), `MphBackend` (stub — deliberately unimplemented until M1 confirms
  the real mph API surface, see `tools/mph_explore.py`).
- `tools/probe_license.py` + `tools/probe_license_driver.py` — M0 license
  concurrency probe. Needs `fixtures/small_waveoptics.mph` (not included —
  see `fixtures/README.md`) and only runs where `mph` + COMSOL are installed.
- `tools/mph_explore.py` — M1 throwaway script to verify the real mph API
  against a real SPR-PCF model before building `MphBackend` on top of it.
  Findings recorded in `fixtures/README.md` and the `MphBackend` docstring.
- `src/simserver/db.py`, `queue.py`, `worker.py`, `cli.py` — M3: SQLite schema,
  atomic model-affinity-preferring claim (`BEGIN IMMEDIATE` + `RETURNING`),
  single worker loop against `FakeBackend`, and a `simserver` CLI
  (`register-model` / `enqueue` / `worker` / `jobs` / `results`) to drive it
  without HTTP.
- `src/simserver/api.py`, `manifest.py`, `storage.py` — M4: FastAPI layer
  (`/models`, `/jobs`, `/jobs/{id}`, `/jobs/{id}/results`, `/queue`,
  `/healthz`). Manifest-driven validation (plan §4): job submission rejects
  unknown/out-of-range parameters, and output *names* resolve to expressions
  from the stored manifest — a client can never supply a raw COMSOL
  expression. `DELETE /jobs/{id}` only handles the cheap queued-job case;
  killing a running job needs the supervisor (M5).

- `src/simserver/supervisor.py` — M5: spawns a fixed configured worker pool
  (one at a time, failing loudly if one dies during its startup grace period
  — no license-driven pool-size discovery, since concurrency was found to be
  effectively unlimited and RAM/CPU is the actual bottleneck), monitors for
  crashed workers and respawns them (waiting for confirmed process exit
  first), enforces a per-model watchdog timeout by killing a worker's whole
  process tree via `psutil` (parent + children, cross-platform), and
  reconciles any job left `running` by a dead/killed worker back onto the
  queue with a capped, backed-off retry (`queue.requeue_or_fail`) so a
  reliably-hanging job fails permanently instead of looping forever. The
  worker itself self-recycles past a configurable RSS threshold
  (`--memory-threshold-mb`). `simserver supervisor --workers N ...` runs it.

- `src/simserver/backend/mph_backend.py` — M6: real `MphBackend`, implemented
  against the confirmed `mph` API (M1 findings + reading `mph`'s own source
  directly, which also confirmed `model.solve(study)`'s signature without
  needing another VM round-trip). Error classification (license-keyword
  heuristic → infrastructure, else solver/validation by phase) is not yet
  verified against a real non-convergent solve or license failure — see the
  module docstring. `tools/mph_backend_smoke.py` exercises the full
  load→set_parameters→build→mesh→solve→evaluate→release contract against a
  real model; not yet run in the VM.

Not yet built: mode selection / sweeps (M7), batches/dataset export (M8),
service install/auth (M9).

## Dev setup

```
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,api]'
pytest
```

Run the API (binds `127.0.0.1` by default via uvicorn's own default host):

```
uvicorn simserver.api:app --reload
```

Run a worker against it (separate process, same `data/jobs.db`):

```
simserver worker
```

Extras: `.[mph]` (only installable in the VM), `.[api]` (FastAPI/uvicorn/python-multipart, for M4).
