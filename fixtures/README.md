# fixtures

`small_waveoptics.mph` needs to be added here before `tools/probe_license.py` /
`probe_license_driver.py` can run. Requirements (plan §1):

- Uses the Wave Optics module (so solving it forces a module checkout, not just
  a base COMSOL session — the two can have different seat counts).
- Coarse mesh, single wavelength. It should solve in seconds, not minutes — the
  probe is measuring license checkout behaviour, not solve time.

`.mph` files are binary and excluded from git (see `.gitignore`); build this
directly in COMSOL in the VM and drop it in this directory.
