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

Not yet built: FastAPI layer (M4), supervisor (M5), the real `MphBackend`
wiring (M6), mode selection / sweeps (M7), batches/dataset export (M8),
service install/auth (M9).

## Dev setup

```
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
pytest
```

Extras: `.[mph]` (only installable in the VM), `.[api]` (FastAPI/uvicorn, for M4).
