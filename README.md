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
  needing another VM round-trip). Verified live in the VM via
  `tools/mph_backend_smoke.py`: the full load→set_parameters→build→mesh→
  solve→evaluate→release contract works against the real fixture, and the
  `neff` values match the raw `mph_explore.py` run exactly. Also confirmed
  live: `mph.start()` spawns `comsolmphserver.exe` as a genuine child process
  of the worker, so the supervisor's existing `psutil` process-tree kill
  already reaches it on a watchdog timeout — no extra release step needed.
  Error classification (license-keyword heuristic → infrastructure, else
  solver/validation by phase) is still unverified against a real
  non-convergent solve or license failure — see the module docstring.

- `src/simserver/mode_selection.py` — M7 (part 1 of 3): `nearest_neff_to_target`
  mode selection, verified live end-to-end (real solve → selection → written
  result matched the expected mode's values exactly). `core_power_fraction`
  is an explicit stub — it needs a domain integration operator no checked-in
  fixture has, and was deliberately not implemented blind.
- Geometry-flag mesh-skip (M7, part 2 of 3) — already implemented since M3,
  now verified live through the real `Worker`/`MphBackend` stack (not just
  `FakeBackend` unit tests or the raw `mph_explore.py` script): a
  non-geometry-only job skipped the rebuild entirely, a geometry-parameter
  job triggered a 0.6s rebuild, consistent with earlier raw findings.
- In-COMSOL sweeps (M7, part 3 of 3) — done. `tools/mph_sweep_explore.py`
  found the real mechanism live: a "Parametric Sweep" study extension
  (`Study.create('Parametric')`, properties `pname`/`plistarr`), solved via
  the normal `model.solve(study)`, with all points' data in a second
  dataset COMSOL creates ("...//Parametric Solutions 1") rather than the
  default one (which only ever holds the last point — a real trap for
  anyone evaluating post-sweep without knowing this). `SolverBackend` grew
  `configure_sweep`/`disable_sweep`; the worker fetches each output once
  (not once per point) and slices the flattened result per sweep point,
  running mode selection independently per point (`results`/`mode_selection`
  rows keyed by `sweep_index`/`sweep_value`). A client requests a sweep by
  passing a list of values for the manifest's `sweep_parameter` instead of a
  single string. `core_power_fraction` remains the one explicit stub in the
  whole M6/M7 arc — still deferred pending a fixture with a real domain
  selection.

Not yet built: batches/dataset export (M8), service install/auth (M9).

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
