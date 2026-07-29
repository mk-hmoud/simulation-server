"""M3 CLI: register models, enqueue jobs, run a worker loop. No HTTP yet (M4)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import db
from . import queue as q
from .backend import FakeBackend
from .worker import Worker

DEFAULT_DB_PATH = Path("data/jobs.db")


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


def cmd_enqueue(args: argparse.Namespace) -> None:
    conn = _connect(args)
    params = json.loads(args.params)
    outputs = json.loads(args.outputs)
    job_id = q.enqueue_job(conn, args.model_id, params, outputs=outputs, priority=args.priority)
    print(f"enqueued job {job_id}")


def cmd_worker(args: argparse.Namespace) -> None:
    conn = _connect(args)
    backend = FakeBackend()  # M3 scope: FakeBackend only; MphBackend wiring is M6
    worker = Worker(conn, backend, worker_id=args.worker_id)
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


def cmd_results(args: argparse.Namespace) -> None:
    conn = _connect(args)
    columns = "job_id, output_name, sweep_index, sweep_value, value_real, value_imag"
    rows = conn.execute(f"SELECT {columns} FROM results WHERE job_id = ? ORDER BY output_name, sweep_index", (args.job_id,))
    for row in rows:
        print(dict(row))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="simserver")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH, help="path to jobs.db")
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
    p.add_argument("--outputs", default="{}", help="JSON dict of {output_name: expression}")
    p.add_argument("--priority", type=int, default=0)
    p.set_defaults(func=cmd_enqueue)

    p = sub.add_parser("worker")
    p.add_argument("--worker-id", default="worker-1")
    p.add_argument("--once", action="store_true", help="process a single job and exit")
    p.set_defaults(func=cmd_worker)

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
