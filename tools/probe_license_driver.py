"""License-concurrency probe driver (plan §1, M0).

Launches probe_license.py instances one at a time, waiting for each to print
"ok" before starting the next. Stops at the first failure, captures the full
exception text, and writes the discovered concurrency limit to
config/license.json.

Usage:
    python tools/probe_license_driver.py [--max N] [--fixture PATH]

Requires `mph` and a COMSOL install — only runs in the VM. This costs real
wall time (each session solves a small model and is held for --hold-seconds),
so run it once, not per supervisor start — the supervisor's own startup probe
(plan §5.4) is the thing that re-checks on every boot, and it can reuse this
same probe_license.py subprocess pattern with a much shorter hold.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE_SCRIPT = REPO_ROOT / "tools" / "probe_license.py"
CONFIG_PATH = REPO_ROOT / "config" / "license.json"

OK_MARKER = ": ok"


def launch_probe(index: int, fixture: str, hold_seconds: float) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(PROBE_SCRIPT),
            str(index),
            "--fixture",
            fixture,
            "--hold-seconds",
            str(hold_seconds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )


def wait_for_ok_or_failure(proc: subprocess.Popen, timeout: float) -> tuple[bool, str]:
    """Read stdout until the ok marker, process exit, or timeout.

    Returns (succeeded, detail) where detail is the combined stdout/stderr text
    captured so far — this is what should get logged for the "which feature"
    diagnosis mentioned in the plan.
    """
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = proc.stdout.readline()
        if line:
            print(line, end="")
            lines.append(line)
            if OK_MARKER in line:
                return True, "".join(lines)
        if proc.poll() is not None:
            break

    if proc.poll() is None:
        proc.kill()
        stderr = proc.stderr.read() if proc.stderr else ""
        return False, "".join(lines) + f"\n[driver] timed out waiting for 'ok'\n{stderr}"

    stderr = proc.stderr.read() if proc.stderr else ""
    return False, "".join(lines) + stderr


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=8, help="upper bound on sessions to try")
    parser.add_argument("--fixture", default="fixtures/small_waveoptics.mph")
    parser.add_argument("--hold-seconds", type=float, default=600.0)
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=180.0,
        help="seconds to wait for each session's 'ok' before treating it as failed",
    )
    args = parser.parse_args()

    if not (REPO_ROOT / args.fixture).exists():
        print(
            f"fixture not found: {args.fixture} — build a coarse-mesh, "
            "single-wavelength Wave Optics model first",
            file=sys.stderr,
        )
        return 2

    held: list[subprocess.Popen] = []
    succeeded_count = 0
    failure_detail = ""

    try:
        for i in range(1, args.max + 1):
            print(f"--- launching session {i} ---", flush=True)
            proc = launch_probe(i, args.fixture, args.hold_seconds)
            ok, detail = wait_for_ok_or_failure(proc, args.startup_timeout)
            if ok:
                held.append(proc)
                succeeded_count = i
            else:
                failure_detail = detail
                print(f"--- session {i} failed, stopping ---", flush=True)
                break
        else:
            print(f"--- reached --max={args.max} without failure; raise --max and re-run ---", flush=True)

        result = {
            "max_concurrent_sessions": succeeded_count,
            "probed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "fixture": args.fixture,
            "failure_detail": failure_detail,
        }
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(result, indent=2))
        print(f"\nwrote {CONFIG_PATH}: max_concurrent_sessions={succeeded_count}")
        return 0
    finally:
        print("cleaning up held sessions...", flush=True)
        for proc in held:
            proc.kill()
        for proc in held:
            proc.wait(timeout=30)


if __name__ == "__main__":
    raise SystemExit(main())
