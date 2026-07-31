"""M7 throwaway exploration: how to configure an in-COMSOL parametric sweep
via mph (plan §7 "Sweeps"). This API surface hasn't been touched yet, unlike
load/build/mesh/solve/evaluate/parameter which M1/M6 already confirmed.

Discovery-only: creates a "Parametric" study-extension feature node and dumps
its real property names/defaults via Node.properties(), rather than guessing
property names (e.g. pname/plistarr) and burning a round-trip if wrong.

Usage:
    python tools/mph_sweep_explore.py fixtures/spr_pcf_side_hole.mph
"""

from __future__ import annotations

import argparse


def dump_tree(node, depth: int = 0, max_depth: int = 2) -> None:
    print("  " * depth + f"name={node.name()!r} tag={node.tag()!r} type={node.type()!r}")
    if depth >= max_depth:
        return
    try:
        for child in node:
            dump_tree(child, depth + 1, max_depth)
    except Exception as exc:  # noqa: BLE001
        print("  " * (depth + 1) + f"<cannot list children: {exc!r}>")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model_path")
    parser.add_argument("--study", default="std1")
    args = parser.parse_args()

    import mph

    client = mph.start(cores=1)
    model = client.load(args.model_path)

    study = model / "studies" / args.study
    print("--- study node tree before sweep setup ---")
    dump_tree(study)

    print("\n--- creating a Parametric study-extension feature ---")
    try:
        sweep = study.create("Parametric")
    except Exception as exc:  # noqa: BLE001
        print(f"study.create('Parametric') FAILED: {exc!r}")
        raise
    print(f"created: name={sweep.name()!r} tag={sweep.tag()!r} type={sweep.type()!r}")

    print("\n--- sweep.properties() (real names/defaults, not guessed) ---")
    for name, value in sweep.properties().items():
        print(f"  {name!r} = {value!r}")

    print("\n--- study node tree after sweep creation ---")
    dump_tree(study)

    sweep.remove()
    client.remove(model)
    print("\ndone — use the property names/shapes above to write the real sweep-setting code,")
    print("don't guess pname/plistarr shapes from general COMSOL API recollection")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
