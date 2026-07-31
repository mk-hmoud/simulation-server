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

  Verified live end-to-end against the real fixture (3-point wavelength
  sweep combined with mode selection): each point's mode selection ran
  independently (6 modes considered each), and the middle point's written
  result (`neff_real`/`neff_imag`) matched an earlier standalone single-point
  solve at the same wavelength to ~13 significant digits.

- `src/simserver/dataset.py` — M8: batches (`POST /batches`, one job per
  `params_list` entry, sharing a `batch_id` — reserved for geometry variation,
  where a rebuild is unavoidable per job anyway, as opposed to a sweep's
  single-job in-COMSOL iteration), `GET /batches/{id}` aggregate status, and
  `GET /batches/{id}/dataset.csv` flattening every job's results into one
  dataframe-ready CSV (one row per job/sweep-point, scalar params as columns,
  each output as a column with a `__imag` column only where actually used).
  Maintenance mode (`POST /admin/drain`/`/admin/resume`) stops workers
  claiming new jobs and, once each finishes its current job, exits the whole
  process — verified live that idling isn't enough to free a COMSOL license
  (it's bound to the process for its lifetime), so the worker must actually
  exit, and the supervisor must not respawn it until resume. Verified locally
  end-to-end via the CLI: a 3-job batch producing a correct flattened CSV,
  and a real 2-worker supervisor cleanly draining (both workers exit, no
  respawn) and resuming (both respawned) on command.

- `src/simserver/config.py`, `api.py` — M9 auth: two-tier static API key
  (plan §9), `SIMSERVER_JOB_API_KEY` / `SIMSERVER_ADMIN_API_KEY` env vars.
  Job key covers everything except model upload and maintenance-mode
  toggling, which need the admin key specifically (admin key also satisfies
  the job tier). `GET /healthz` never requires a key. Either tier is disabled
  (dev mode) if its env var is unset — verified locally both via `TestClient`
  and a real `uvicorn` process + `curl` (401 without a key, 401 for the wrong
  tier, 200 for the right one). NSSM service install and the tunnel are
  deployment steps for you to run on the VM — see "Deployment" below.

- `src/simserver/web.py`, `auth.py`, `deps.py`, `templates/`, `static/` —
  server-rendered web client (Jinja2 + minimal vanilla JS, no SPA/build
  step) so other researchers in the faculty can submit and monitor
  simulations from a browser, not just via the JSON API/CLI. A separate
  auth layer from the JSON API's static key scheme: per-user accounts
  (username/password, PBKDF2 via stdlib `hashlib`, no new dependency),
  session cookies, admin-created accounts only (`simserver users create`,
  or the Users page for an already-logged-in admin — no public
  self-registration). Jobs/batches submitted through the UI go through the
  *exact same* `manifest.validate_params`/`resolve_outputs`/
  `queue.enqueue_job` the JSON API uses, stamped with the submitting user's
  `owner_user_id`, so "my jobs" vs. the shared queue is just a filter, not a
  separate code path. A swept job's detail page draws a small canvas line
  chart per output (value vs. sweep value) with vanilla JS — no charting
  library/CDN, since researchers reach this over a private Tailscale link
  with no reason to depend on external fetches. Verified live via a real
  `uvicorn` process: login → dashboard → model detail → job submission →
  worker processes it → job detail page shows the result, with the correct
  `owner_user_id`; non-admin correctly blocked (403) from `/ui/admin`,
  `/ui/users`, `/ui/models/new`.

## Deployment (M9)

Everything below runs **on the VM**, as Administrator where noted. Not yet
executed — these are the steps to run when ready to actually deploy.

### 1. Set the API keys

Generate two random strings (e.g. `python -c "import secrets; print(secrets.token_hex(32))"`
twice) and set them as persistent environment variables (System Properties →
Environment Variables, or `setx`) so services started by NSSM inherit them:

```powershell
setx SIMSERVER_JOB_API_KEY   "<job-key>" /M
setx SIMSERVER_ADMIN_API_KEY "<admin-key>" /M
```

`/M` sets it machine-wide (needed since NSSM services don't inherit your
interactive user's env otherwise). Requires a new shell / service restart to
take effect.

### 2. Install NSSM services (api + supervisor; workers are not services)

Download NSSM from nssm.cc, extract `win64\nssm.exe` somewhere on `PATH`.

```powershell
nssm install SimServerApi "C:\simserver\.venv\Scripts\python.exe" "-m uvicorn simserver.api:app --host 127.0.0.1 --port 8000"
nssm set SimServerApi AppDirectory C:\simserver
nssm set SimServerApi AppStdout C:\simserver\data\logs\api.log
nssm set SimServerApi AppStderr C:\simserver\data\logs\api.log
nssm start SimServerApi

nssm install SimServerSupervisor "C:\simserver\.venv\Scripts\python.exe" "-m simserver.cli supervisor --workers N --backend mph"
nssm set SimServerSupervisor AppDirectory C:\simserver
nssm set SimServerSupervisor AppStdout C:\simserver\data\logs\supervisor.log
nssm set SimServerSupervisor AppStderr C:\simserver\data\logs\supervisor.log
nssm start SimServerSupervisor
```

Replace `N` with a worker count sized from measured per-session RAM (plan
§11 open question — still not measured; start with `--workers 1` and watch
`Task Manager`/`Get-Process comsolmphserver` RSS before increasing).
`AppEnvironmentExtra` isn't needed if step 1's `setx /M` was used; set it
instead (`nssm set SimServerApi AppEnvironmentExtra KEY=value`) if you'd
rather scope the keys to just these services.

The supervisor's own `--memory-threshold-mb`/`--watchdog-timeout`/etc. flags
(see `simserver supervisor --help`) are also worth setting explicitly here
rather than relying on defaults tuned for dev.

### 3. Reach the API — Tailscale

Never expose the VM's port 8000 directly (the API binds `127.0.0.1` for
exactly this reason). Chosen approach: **Tailscale**, so other researchers
in the faculty can reach it too, not just a single tunnel operator — each
researcher installs the Tailscale client on their own machine and is added
as a peer; once that's done they can reach `http://<vm-tailscale-ip>:8000`
(and `/ui/...` for the web client) from anywhere with internet access, no
physical/campus-network requirement. Adding peers is a one-time step per
researcher, done outside this codebase (Tailscale admin console).

The API key (step 1) is a second layer on top of the tunnel "for the case
where the tunnel is shared" (plan §9) — keep it even with Tailscale. The web
client's per-user accounts (below) are a separate, additional layer on top
of that, specific to the browser UI.

### 4. Create researcher accounts for the web client

```powershell
python -m simserver.cli users create <username> --admin   # for yourself / other admins
python -m simserver.cli users create <username>            # for each researcher
```

Prompts for a password (hidden input) if `--password` isn't given. Point
researchers at `http://<vm-tailscale-ip>:8000/ui/login`. Admins can also
create accounts from the Users page once logged in, so this doesn't have to
stay a CLI-only step.

## Dev setup

```
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,api]'
pytest
```

Run the API + web client (binds `127.0.0.1` by default via uvicorn's own
default host):

```
uvicorn simserver.api:app --reload
```

Create a local user and open the web client at `http://127.0.0.1:8000/ui/login`:

```
simserver users create myself --admin --password devpassword
```

Run a worker against it (separate process, same `data/jobs.db`):

```
simserver worker
```

Extras: `.[mph]` (only installable in the VM), `.[api]` (FastAPI/uvicorn/python-multipart/jinja2, for M4 + the web client).
