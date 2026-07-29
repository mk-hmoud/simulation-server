"""M3 CLI: register models, enqueue jobs, run a worker loop. No HTTP yet (M4)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import config, db
from . import queue as q
from .backend import FakeBackend
from .worker import Worker


def _connect(args: argparse.Namespace):
    conn = db.connect(args.db)
    db.init_db(conn)
    return conn


def cmd_init_db(args: argparse.Namespace) -> None:
    _connect(args)
    print(f"initialized {args.db}")


def cmd_register_model(args: argparse.Namespace) -> None:
    conn = _connect(args)
    manifest = json.loads(Path(args.manifest).read_text()) if args.manifest else {}
    q.register_model(conn, args.model_id, args.path, manifest)
    print(f"registered model {args.model_id!r} -> {args.path}")


def _load_json_arg(inline: str, file_path: str | None) -> dict:
    # --*-file wins over the inline string when both/neither are given, since
    # PowerShell mangles embedded double quotes when passing JSON to a native
    # exe (backslash-escaping works but is painful) — a file sidesteps it
    if file_path:
        # utf-8-sig strips a BOM if present (e.g. PowerShell's `>`/Out-File
        # default encoding) and is a no-op otherwise — plain read_text() would
        # choke on a BOM-prefixed file with a confusing JSONDecodeError
        return json.loads(Path(file_path).read_text(encoding="utf-8-sig"))
    return json.loads(inline)


def cmd_enqueue(args: argparse.Namespace) -> None:
    conn = _connect(args)
    params = _load_json_arg(args.params, args.params_file)

    if args.outputs is None and args.outputs_file is None:
        # match the HTTP API's default (manifest.resolve_outputs(requested=None)):
        # omitting outputs entirely means "all of them", not "none of them"
        model_row = q.get_model(conn, args.model_id)
        if model_row is None:
            raise SystemExit(f"unknown model_id {args.model_id!r}")
        outputs = json.loads(model_row["manifest_json"]).get("outputs", {})
    else:
        outputs = _load_json_arg(args.outputs or "{}", args.outputs_file)

    job_id = q.enqueue_job(conn, args.model_id, params, outputs=outputs, priority=args.priority)
    print(f"enqueued job {job_id}")


def cmd_worker(args: argparse.Namespace) -> None:
    conn = _connect(args)
    if args.backend == "mph":
        from .backend import MphBackend

        backend = MphBackend(cores=args.cores)  # still a stub until M6
    else:
        backend = FakeBackend()
    memory_threshold_bytes = (
        int(args.memory_threshold_mb * 1024 * 1024) if args.memory_threshold_mb is not None else None
    )
    worker = Worker(
        conn,
        backend,
        worker_id=args.worker_id,
        memory_threshold_bytes=memory_threshold_bytes,
    )
    if args.once:
        processed = worker.process_one()
        print("processed a job" if processed else "queue was empty")
    else:
        print(f"worker {args.worker_id!r} running against {args.db} (Ctrl-C to stop)")
        worker.run_forever()


def cmd_jobs(args: argparse.Namespace) -> None:
    conn = _connect(args)
    columns = "id, model_id, status, priority, worker_id, created_at, started_at, finished_at, error_class, error_message"
    for row in conn.execute(f"SELECT {columns} FROM jobs ORDER BY id"):
        print(dict(row))


def cmd_supervisor(args: argparse.Namespace) -> None:
    from .supervisor import Supervisor, SupervisorConfig

    supervisor_config = SupervisorConfig(
        db_path=args.db,
        worker_count=args.workers,
        backend=args.backend,
        memory_threshold_mb=args.memory_threshold_mb,
        default_watchdog_timeout_s=args.watchdog_timeout,
        poll_interval_s=args.poll_interval,
        startup_grace_s=args.startup_grace,
        max_attempts=args.max_attempts,
        retry_backoff_s=args.retry_backoff,
    )
    print(f"supervisor: bringing up {args.workers} worker(s) against {args.db} (Ctrl-C to stop)")
    Supervisor(supervisor_config).run_forever()


def cmd_results(args: argparse.Namespace) -> None:
    conn = _connect(args)
    columns = "job_id, output_name, sweep_index, sweep_value, value_real, value_imag"
    rows = conn.execute(f"SELECT {columns} FROM results WHERE job_id = ? ORDER BY output_name, sweep_index", (args.job_id,))
    for row in rows:
        print(dict(row))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simserver")
    parser.add_argument("--db", type=Path, default=config.DB_PATH, help="path to jobs.db")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("register-model")
    p.add_argument("model_id")
    p.add_argument("path")
    p.add_argument("--manifest", help="path to a JSON manifest file")
    p.set_defaults(func=cmd_register_model)

    p = sub.add_parser("enqueue")
    p.add_argument("model_id")
    p.add_argument("--params", default="{}", help="JSON dict of parameter overrides")
    p.add_argument("--params-file", help="path to a JSON file, alternative to --params (avoids shell-quoting pain)")
    p.add_argument(
        "--outputs",
        default=None,
        help="JSON dict of {output_name: expression}; omit (with --outputs-file too) for all manifest outputs",
    )
    p.add_argument("--outputs-file", help="path to a JSON file, alternative to --outputs")
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("worker")
    p.add_argument("--worker-id", default="worker-1")
    p.add_argument("--once", action="store_true", help="process a single job and exit")
    p.add_argument("--backend", choices=["fake", "mph"], default="fake")
    p.add_argument("--cores", type=int, default=None, help="only used with --backend mph")
    p.add_argument(
        "--memory-threshold-mb",
        type=float,
        default=None,
        help="exit for recycle once RSS exceeds this (plan §5.3); omit to never self-recycle",
    )
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("supervisor")
    p.add_argument("--workers", type=int, default=1, help="fixed pool size (license isn't the bottleneck, RAM/CPU is)")
    p.add_argument("--backend", choices=["fake", "mph"], default="fake")
    p.add_argument("--memory-threshold-mb", type=float, default=2048.0)
    p.add_argument("--watchdog-timeout", type=float, default=600.0, help="default if a model manifest omits timeout_seconds")
    p.add_argument("--poll-interval", type=float, default=2.0)
    p.add_argument("--startup-grace", type=float, default=15.0, help="seconds to confirm each worker survives startup")
    p.add_argument("--max-attempts", type=int, default=2, help="total attempts before a job is permanently failed")
    p.add_argument("--retry-backoff", type=float, default=30.0)
    p.set_defaults(func=cmd_supervisor)

    p = sub.add_parser("jobs")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("results")
    p.add_argument("job_id", type=int)
    p.set_defaults(func=cmd_results)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
