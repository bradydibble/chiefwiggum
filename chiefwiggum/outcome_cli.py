"""CLI entrypoint for writing WorkerOutcome files from a shell worker.

Usage (from inside ralph_loop.sh):

    python3 -m chiefwiggum.outcome_cli \\
        --ralph-id "$RALPH_ID" \\
        --status success \\
        --task-id "$TASK_ID" \\
        --commit "$COMMIT_SHA"

This is deliberately separate from the main `wig` CLI because it must
never fail noisily: if a worker's exit trap hits any error writing the
outcome file, the daemon can still fall back to reading the worker's
last-known log. So this script catches everything and writes something,
even if partial.
"""

from __future__ import annotations

import argparse
import sys

from chiefwiggum.outcome import WorkerExitStatus, WorkerOutcome, iso_now, write_outcome


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Write a worker outcome file.")
    p.add_argument("--ralph-id", required=True)
    p.add_argument("--status", required=True, choices=[s.value for s in WorkerExitStatus])
    p.add_argument("--task-id", default=None)
    p.add_argument("--commit", default=None)
    p.add_argument("--error-category", default=None)
    p.add_argument("--error-message", default=None)
    p.add_argument("--started-at", default=None,
                   help="ISO timestamp; defaults to now if omitted.")
    p.add_argument("--ended-at", default=None,
                   help="ISO timestamp; defaults to now.")
    p.add_argument("--loops-run", type=int, default=0)
    p.add_argument("--total-cost-usd", type=float, default=0.0)
    p.add_argument("--log-path", default=None)
    args = p.parse_args(argv)

    outcome = WorkerOutcome(
        ralph_id=args.ralph_id,
        status=WorkerExitStatus(args.status),
        task_id=args.task_id or None,
        commit_sha=args.commit or None,
        error_category=args.error_category or None,
        error_message=args.error_message or None,
        started_at=args.started_at or None,
        ended_at=args.ended_at or iso_now(),
        loops_run=args.loops_run,
        total_cost_usd=args.total_cost_usd,
        log_path=args.log_path or None,
    )
    try:
        path = write_outcome(outcome)
        print(str(path))
        return 0
    except Exception as e:
        # Last-resort: print to stderr so it shows up in the worker's log,
        # but don't exit non-zero — the worker's set -e would then kill
        # the exit trap itself.
        print(f"outcome_cli: failed to write outcome: {e}", file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
