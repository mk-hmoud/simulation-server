"""Throwaway mph API exploration script (plan §3 note, M1).

The plan's SolverBackend/MphBackend signatures are written from documentation,
not verified against the installed mph version. Run this against a real
SPR-PCF model in the VM before writing any code on top of mph — it exercises
load / set one parameter / solve / evaluate, and dumps the actual object API
so differences from the assumed signatures show up immediately.

Usage:
    python tools/mph_explore.py path/to/spr_pcf.mph \\
        --param lambda0=1550[nm] \\
        --expression "real(ewfd.neff)"

Requires `mph` and a COMSOL install — only runs in the VM.
"""

from __future__ import annotations

import argparse
import time


def dump_api(label: str, obj: object) -> None:
    print(f"\n--- {label}: type={type(obj)!r} ---")
    members = [m for m in dir(obj) if not m.startswith("_")]
    print(", ".join(members))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="repeatable, e.g. --param lambda0=1550[nm]",
    )
    parser.add_argument("--expression", default=None, help="e.g. real(ewfd.neff)")
    parser.add_argument("--study", default=None, help="study tag to solve, if the model has more than one")
    parser.add_argument("--cores", type=int, default=1)
    args = parser.parse_args()

    import mph

    print(f"mph version: {getattr(mph, '__version__', 'unknown')}")

    t0 = time.monotonic()
    client = mph.start(cores=args.cores)
    print(f"client started in {time.monotonic() - t0:.1f}s")
    dump_api("client", client)

    t0 = time.monotonic()
    model = client.load(args.model_path)
    print(f"model loaded in {time.monotonic() - t0:.1f}s")
    dump_api("model", model)

    for kv in args.param:
        name, _, value = kv.partition("=")
        print(f"setting parameter {name} = {value}")
        model.parameter(name, value)

    t0 = time.monotonic()
    model.build()
    print(f"build() took {time.monotonic() - t0:.1f}s")

    t0 = time.monotonic()
    model.mesh()
    print(f"mesh() took {time.monotonic() - t0:.1f}s")

    t0 = time.monotonic()
    model.solve(args.study) if args.study else model.solve()
    print(f"solve() took {time.monotonic() - t0:.1f}s")

    # dump the model's actual node names — the plan's manifest examples (ewfd.neff,
    # sel_core, etc.) are illustrative, not verified against any real model, so this
    # is how to find the real names before writing a manifest for this model
    for kind in ("physics", "studies", "datasets", "solutions", "components", "selections", "functions"):
        try:
            names = getattr(model, kind)()
            print(f"{kind}: {names}")
        except Exception as exc:  # noqa: BLE001
            print(f"{kind}: <failed to list: {exc!r}>")

    if args.expression:
        try:
            result = model.evaluate(args.expression)
            print(f"\nevaluate({args.expression!r}) -> {result!r} (type={type(result)!r}, shape={getattr(result, 'shape', None)})")
            # if this is a Wave Optics mode analysis, evaluate() may return multiple
            # eigenmodes at once — check whether the result shape lets core-power-fraction
            # mode selection (plan §4) be computed from evaluate() alone, or whether it
            # needs a lower-level Java-API escape hatch (plan §11, open question)
        except Exception as exc:  # noqa: BLE001 - report and keep going so the node dump above is still useful
            print(f"\nevaluate({args.expression!r}) FAILED: {exc!r}")

    client.remove(model)
    print("\ndone — record any signature differences from the plan's assumed API in the plan doc")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
