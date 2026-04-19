"""Worker → daemon outcome protocol.

Every Ralph worker process writes exactly ONE `outcome.json` file at exit
to communicate what happened to its claimed task. The daemon consumes
these files on its reconcile tick and performs the corresponding idempotent
state transitions in the database.

This replaces the old shell-based `wig complete` / `wig claim` /
`wig release` calls inside the worker. That IPC channel was the source of
repeated `set -e` crashes and JSON-vs-string comparison bugs.

Protocol invariants:
  * ONE outcome.json per worker process. Atomic rename into place so the
    daemon never reads a partial file.
  * Idempotent on the daemon side. If the daemon crashes mid-consume and
    re-reads the file, complete_task/fail_task must be safe to retry.
  * Missing outcome + dead PID = worker crashed; daemon releases the
    claim and optionally re-queues the task.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

logger = logging.getLogger(__name__)


class WorkerExitStatus(str, Enum):
    """What happened in this worker's single-task lifecycle."""

    SUCCESS = "success"           # task completed cleanly (RALPH_STATUS COMPLETE)
    FAILED = "failed"             # task marked failed by Claude (RALPH_STATUS FAIL)
    NO_TASK = "no_task"           # worker spawned but claimed no task
    TIMEOUT = "timeout"           # hit MAX_LOOPS or hour-long Claude timeout
    CIRCUIT_OPEN = "circuit_open" # circuit breaker tripped on repeated errors
    ABORTED = "aborted"           # interrupted by signal or `wig cancel`
    CRASHED = "crashed"           # uncaught shell/Python exception


@dataclass
class WorkerOutcome:
    """Structured result of one worker's single-task run.

    Written by ralph_loop.sh at exit; consumed by the chiefwiggum daemon.
    """

    ralph_id: str
    status: WorkerExitStatus
    task_id: str | None = None          # populated when a task was claimed/worked
    commit_sha: str | None = None       # populated on SUCCESS
    error_category: str | None = None   # populated on FAILED/CRASHED/TIMEOUT
    error_message: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    loops_run: int = 0
    total_cost_usd: float = 0.0
    log_path: str | None = None         # full path to the worker's log for inspection
    metadata: dict = field(default_factory=dict)

    def to_json(self) -> str:
        d = asdict(self)
        d["status"] = self.status.value
        return json.dumps(d, indent=2)

    @classmethod
    def from_json(cls, text: str) -> WorkerOutcome:
        raw = json.loads(text)
        raw["status"] = WorkerExitStatus(raw["status"])
        # Drop unknown keys so older workers can still be read by newer
        # daemons without raising TypeError on unexpected kwargs.
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in raw.items() if k in known})


def get_outcome_path(ralph_id: str) -> Path:
    """Location of a worker's outcome file.

    Uses the same ralphs/ directory tree the daemon already scans for
    PID files and status files.
    """
    from chiefwiggum.paths import get_paths
    paths = get_paths()
    paths.ensure_dirs()
    return paths.ralphs_dir / f"{ralph_id}.outcome.json"


def write_outcome(outcome: WorkerOutcome) -> Path:
    """Atomically write a WorkerOutcome to disk.

    Writes to a sibling temp file in the same directory, fsyncs, then
    renames into place so a daemon reading concurrently never sees a
    half-written JSON.
    """
    target = get_outcome_path(outcome.ralph_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    # NamedTemporaryFile on the same filesystem so os.replace() is atomic.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{outcome.ralph_id}.outcome.",
        suffix=".json.tmp",
        dir=str(target.parent),
    )
    try:
        with os.fdopen(fd, "w") as fp:
            fp.write(outcome.to_json())
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_name, target)
    except Exception:
        # Best effort cleanup; don't hide the real error.
        try:
            Path(tmp_name).unlink()
        except FileNotFoundError:
            pass
        raise

    return target


def read_outcome(path: Path) -> WorkerOutcome | None:
    """Read an outcome file. Returns None if the file is missing or malformed.

    A malformed file is logged and treated as "no outcome" — the daemon
    will fall back to the same recovery path it uses when the file is
    missing (worker crashed), so bad data can't hang the reconcile loop.
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Could not read outcome %s: %s", path, e)
        return None

    try:
        return WorkerOutcome.from_json(text)
    except Exception as e:
        logger.warning("Malformed outcome at %s: %s", path, e)
        return None


def list_pending_outcomes() -> list[Path]:
    """Return all outcome files the daemon hasn't consumed yet."""
    from chiefwiggum.paths import get_paths
    paths = get_paths()
    if not paths.ralphs_dir.exists():
        return []
    return sorted(paths.ralphs_dir.glob("*.outcome.json"))


def consume_outcome(path: Path) -> None:
    """Delete the outcome file after successful consumption."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def iso_now() -> str:
    """UTC ISO timestamp suitable for outcome.started_at / ended_at."""
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
