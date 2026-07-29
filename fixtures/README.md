# fixtures

## spr_pcf_side_hole.mph

Real example SPR-PCF model (side-hole design, built in COMSOL 6.1). Checked into
git as an exception to the general `*.mph` ignore rule — this is the model used
for M1 exploration (`tools/mph_explore.py`) and, eventually, the first model
registered through the manifest system (plan §4).

### M1 findings (mph 1.3.1, COMSOL 6.2, run via tools/mph_explore.py)

- `client.load` / `model.build()` / `.mesh()` / `.solve()` / `.evaluate()` all
  work as the plan assumed. `build()`+`mesh()` are near-instant here (mesh
  already cached in the file); `solve()` ~7-9s.
- Physics interface tag is **`emw`**, not `ewfd` as the plan's illustrative
  manifest example used — e.g. `real(emw.neff)`, not `real(ewfd.neff)`. Tag
  depends on the model/COMSOL version; don't hardcode a physics prefix
  anywhere, always read it from the model (`model.physics()` + `Node.tag()`).
- Study 1 (`std1`) is solution type `Eigenfrequency` and returns **6 eigenmodes**
  per solve. `model.evaluate('real(emw.neff)')` returns a plain `numpy.ndarray`
  of shape `(6,)`, one value per mode — confirms (plan §11 open question) that
  `evaluate()` alone is enough to retrieve all mode data; no need to drop to
  the Java API for that part. Values for this model/params cluster tightly
  (~1.4388-1.4429), i.e. genuinely ambiguous without a selection strategy.
- **`model.selections()` is empty — this model has no named domain selections
  at all.** The manifest's `core_power_fraction` mode-selection strategy (plan
  §4) needs a domain selection (e.g. `sel_core`) to integrate mode power over,
  which doesn't exist here yet. **Deferred**: this fixture can't drive
  `core_power_fraction` mode selection until a core-domain selection (and an
  integration coupling operator over it) is added to the model in the COMSOL
  GUI. Until then, mode-selection work (M7) should implement/test against the
  nearest-neff-to-target fallback instead, or use a different fixture that
  already has the selection.
- `model.parameters()` confirmed: returns `{name: value_str}` as entered in the
  GUI (units included, e.g. `'1.8[um]'`), not evaluated. `model.parameter(name,
  value)` sets one. Real parameter names on this model, and their mapping to
  the plan's illustrative manifest names:

  | real name | plan's example name | role |
  |---|---|---|
  | `l` (`0.65[um]`) | `lambda0` | wavelength — non-geometry, sweep parameter |
  | `na` (`1.33`) | `n_analyte` | analyte index — non-geometry |
  | `p` (`1.8[um]`) | `pitch` | hole pitch — geometry |
  | `d1`/`d2`/`d3`/`dc` (fractions of `p`) | `d_hole` | hole diameters — geometry |
  | `tg` (`40[nm]`) | `t_gold` | gold coating thickness — geometry |

  `A1..B3`/`neff1` are a silica Sellmeier dispersion formula; `einf..kau` are a
  Drude-Lorentz gold dispersion model. Both are derived/material parameters,
  not job-facing inputs — a manifest for this model should not expose them.
- Geometry-flag mesh-skip optimization (plan §4 "the geometry flag matters") is
  real but modest on this fixture: changing `l` (non-geometry) left `mesh()` at
  0.0s; changing `p` (geometry) pushed it to 0.4s. This is a *coarse* fixture
  used for cheap iteration — expect a much bigger gap on a real fine-mesh PCF
  cross-section, which is exactly why the plan calls this out as high-value.

## small_waveoptics.mph

Needs to be added here before `tools/probe_license.py` /
`probe_license_driver.py` can run. Requirements (plan §1):

- Uses the Wave Optics module (so solving it forces a module checkout, not just
  a base COMSOL session — the two can have different seat counts).
- Coarse mesh, single wavelength. It should solve in seconds, not minutes — the
  probe is measuring license checkout behaviour, not solve time.

`.mph` files are binary and excluded from git (see `.gitignore`); build this
directly in COMSOL in the VM and drop it in this directory.
