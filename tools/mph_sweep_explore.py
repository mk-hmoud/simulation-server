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
import time


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
    parser.add_argument("--sweep-param", default="l")
    parser.add_argument("--values", nargs="+", default=["0.6[um]", "0.8[um]", "1.0[um]"])
    parser.add_argument("--expression", default="real(emw.neff)")
    args = parser.parse_args()

    import mph

    client = mph.start(cores=1)
    model = client.load(args.model_path)

    # model/'studies'/name matches by NAME (the GUI label), not tag — passing
    # a tag like "std1" silently builds a reference to a non-existent node
    # instead of erroring (Node() construction is lazy/unchecked), so look it
    # up by tag explicitly instead of assuming name == tag
    study = None
    for candidate in model / "studies":
        if candidate.tag() == args.study:
            study = candidate
            break
    if study is None:
        available = [(c.name(), c.tag()) for c in model / "studies"]
        raise SystemExit(f"no study with tag {args.study!r} found; available (name, tag): {available}")

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

    print(f"\n--- setting pname={[args.sweep_param]!r}, plistarr=[{args.values!r}] ---")
    try:
        sweep.property("pname", [args.sweep_param])
        sweep.property("plistarr", [list(args.values)])
        print("set ok. properties now:")
        for name in ("pname", "plistarr", "punit"):
            print(f"  {name!r} = {sweep.property(name)!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"setting pname/plistarr FAILED: {exc!r}")
        sweep.remove()
        client.remove(model)
        raise

    print("\n--- solving the study (with sweep active) ---")
    t0 = time.monotonic()
    try:
        # pass the resolved Node, not args.study (a tag string) — model.solve()
        # treats a plain str as a NAME lookup, not a tag lookup, and "std1" is
        # the tag while the name is "Study 1"; this bit us the first attempt
        model.solve(study)
    except Exception as exc:  # noqa: BLE001
        print(f"solve FAILED: {exc!r}")
        raise
    print(f"solve took {time.monotonic() - t0:.1f}s")

    print("\n--- datasets/solutions after the sweep solve (was there just one before?) ---")
    for kind in ("datasets", "solutions"):
        names = getattr(model, kind)()
        print(f"{kind}: {names}")
        for child in model / kind:
            print(f"    name={child.name()!r} tag={child.tag()!r} type={child.type()!r}")

    print(f"\n--- evaluate({args.expression!r}) with no dataset/outer= (default) ---")
    result = model.evaluate(args.expression)
    print(f"type={type(result)!r} shape={getattr(result, 'shape', None)!r}")
    print(result)

    for i in range(len(args.values)):
        print(f"\n--- evaluate({args.expression!r}, outer={i}) ---")
        try:
            result_i = model.evaluate(args.expression, outer=i)
            print(f"shape={getattr(result_i, 'shape', None)!r} -> {result_i!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"evaluate(outer={i}) FAILED: {exc!r}")

    # if a dataset besides the original "Study 1//Solution 1" now exists
    # (e.g. a "Parametric Solutions" node), try evaluating against it directly
    # instead of guessing at outer= semantics against the wrong dataset
    for name in model.datasets():
        if "Solution 1" not in name or "Parametric" in name:
            print(f"\n--- evaluate({args.expression!r}, dataset={name!r}) ---")
            try:
                result_ds = model.evaluate(args.expression, dataset=name)
                print(f"shape={getattr(result_ds, 'shape', None)!r} -> {result_ds!r}")
            except Exception as exc:  # noqa: BLE001
                print(f"evaluate(dataset={name!r}) FAILED: {exc!r}")

    sweep.remove()
    client.remove(model)
    print("\ndone")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
