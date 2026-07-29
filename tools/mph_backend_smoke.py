"""M6 smoke test: exercise the real MphBackend's SolverBackend contract
end-to-end against a real model, before trusting it under the worker/queue.

Usage:
    python tools/mph_backend_smoke.py fixtures/spr_pcf_side_hole.mph \\
        --param l=0.8[um] --param na=1.33 \\
        --expression real(emw.neff) --expression imag(emw.neff)

Requires `mph` and a COMSOL install — only runs in the VM.
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from simserver.backend import BackendError, MphBackend


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path")
    parser.add_argument("--param", action="append", default=[], metavar="NAME=VALUE")
    parser.add_argument("--expression", action="append", default=[])
    parser.add_argument("--study", default=None)
    parser.add_argument("--cores", type=int, default=1)
    args = parser.parse_args()

    backend = MphBackend(cores=args.cores)

    def step(label: str, fn):
        t0 = time.monotonic()
        try:
            result = fn()
        except BackendError as exc:
            print(f"{label}: FAILED [{exc.error_class.value}] {exc}")
            raise
        print(f"{label}: ok ({time.monotonic() - t0:.1f}s)" + (f" -> {result!r}" if result is not None else ""))
        return result

    handle = step("load", lambda: backend.load(Path(args.model_path)))

    params = dict(kv.split("=", 1) for kv in args.param)
    if params:
        step("set_parameters", lambda: backend.set_parameters(handle, params))

    step("build_geometry", lambda: backend.build_geometry(handle))
    step("mesh", lambda: backend.mesh(handle))
    step("solve", lambda: backend.solve(handle, args.study))

    for expr in args.expression:
        step(f"evaluate({expr!r})", lambda expr=expr: backend.evaluate(handle, expr))

    step("release", lambda: backend.release(handle))
    print("\nsmoke test passed — MphBackend's contract methods all work against a real model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
