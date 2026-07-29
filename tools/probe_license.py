"""Single-session license probe (plan §1, M0).

Starts one mph/COMSOL session, loads a small Wave Optics fixture, solves it
(forcing a module checkout, not just a base session), then holds the seat for
a while. Meant to be launched by probe_license_driver.py, which starts many of
these one at a time and watches for the first one that fails.

Run directly for a one-off check:
    python tools/probe_license.py 1

Requires `mph` and a COMSOL install — only runs in the VM.
"""

from __future__ import annotations

import argparse
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=int, help="1-based session index, for logging only")
    parser.add_argument(
        "--fixture",
        default="fixtures/small_waveoptics.mph",
        help="coarse-mesh, single-wavelength Wave Optics model — "
        "small so the probe measures checkout behaviour, not solve time",
    )
    parser.add_argument(
        "--hold-seconds",
        type=float,
        default=600.0,
        help="how long to hold the seat after a successful solve",
    )
    args = parser.parse_args()

    import mph  # imported lazily so --help works without mph/COMSOL installed

    print(f"session {args.index}: starting client", flush=True)
    client = mph.start(cores=1)
    try:
        print(f"session {args.index}: loading {args.fixture}", flush=True)
        model = client.load(args.fixture)
        print(f"session {args.index}: solving", flush=True)
        model.solve()
        print(f"session {args.index}: ok", flush=True)
    except Exception as exc:  # noqa: BLE001 - broad on purpose, we want the raw Java-layer text
        # the exception text usually names the feature that could not be checked
        # out, which is what distinguishes a base-seat limit from a module limit
        print(f"session {args.index}: FAILED: {exc!r}", file=sys.stderr, flush=True)
        raise

    time.sleep(args.hold_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
