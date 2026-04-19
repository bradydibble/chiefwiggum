"""Single source of truth for RALPH_STATUS block parsing.

Claude Code emits a block like this when a ralph finishes a task:

    ---RALPH_STATUS---
    STATUS: COMPLETE
    EXIT_SIGNAL: true
    TASK_ID: task-52-verification-steps
    COMMIT: 7c721147bf52e4a0be6612bc6d5baa9ccb6ad2e7
    VERIFICATION: ...
    ---END_RALPH_STATUS---

Historically there were TWO parsers for this block: a bash/grep
implementation in `chiefwiggum/scripts/lib/response_analyzer.sh` and a
Python implementation in `chiefwiggum/spawner.py:check_task_completion`.
They drifted. The shell version silently fell back to scraping `task-N`
out of the last commit message when the block was present but a later
extraction step was wrong — which meant `wig complete task-52` instead
of `wig complete task-52-verification-steps`, which failed, which
crashed workers.

This module is the ONLY parser. The shell worker dumps Claude's output
unmodified into a file; the daemon reads that file through this module.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# A RALPH_STATUS block: tolerates leading whitespace, trailing whitespace,
# and greedy content between the bookend markers. DOTALL so '.' matches
# newlines.
_BLOCK_RE = re.compile(
    r"---RALPH_STATUS---\s*(.*?)\s*---END_RALPH_STATUS---",
    re.DOTALL,
)

# Each field: KEY: value (no colons in key, trailing whitespace stripped).
_FIELD_RE = re.compile(r"^\s*([A-Z_][A-Z0-9_]*)\s*:\s*(.+?)\s*$", re.MULTILINE)


@dataclass
class RalphStatus:
    """Parsed contents of a RALPH_STATUS block.

    All fields optional because Claude sometimes omits them. Consumers
    should branch on `.status`/`.exit_signal`/`.task_id` presence.
    """

    status: str | None = None            # "COMPLETE" | "FAIL" | "CONTINUE" | ...
    exit_signal: bool | None = None      # parsed from "true"/"false"
    task_id: str | None = None           # full stable id, e.g. "task-52-verification-steps"
    commit_sha: str | None = None
    verification: str | None = None
    raw_fields: dict[str, str] | None = None  # everything else, keyed by raw name

    @property
    def is_complete(self) -> bool:
        if self.status and self.status.upper() == "COMPLETE":
            return True
        if self.exit_signal is True:
            return True
        return False

    @property
    def is_failure(self) -> bool:
        if self.status and self.status.upper() in {"FAIL", "FAILED", "BLOCKED"}:
            return True
        return False


def parse_ralph_status(text: str) -> RalphStatus | None:
    """Return the first RALPH_STATUS block in `text`, parsed.

    Returns None if no block is present. Always returns SOMETHING when a
    block exists, even if fields are missing or malformed — downstream
    callers can decide what to do about partial data.
    """
    match = _BLOCK_RE.search(text)
    if not match:
        return None

    body = match.group(1)
    fields: dict[str, str] = {}
    for m in _FIELD_RE.finditer(body):
        fields[m.group(1).upper()] = m.group(2).strip()

    status = fields.get("STATUS")
    exit_signal: bool | None = None
    if "EXIT_SIGNAL" in fields:
        v = fields["EXIT_SIGNAL"].lower()
        if v in {"true", "yes", "1"}:
            exit_signal = True
        elif v in {"false", "no", "0"}:
            exit_signal = False

    task_id = fields.get("TASK_ID") or None
    if task_id == "null" or task_id == "":
        task_id = None

    commit_sha = fields.get("COMMIT") or None
    if commit_sha == "null" or commit_sha == "":
        commit_sha = None

    verification = fields.get("VERIFICATION") or None

    return RalphStatus(
        status=status,
        exit_signal=exit_signal,
        task_id=task_id,
        commit_sha=commit_sha,
        verification=verification,
        raw_fields=fields,
    )


def parse_ralph_status_from_file(path: Path) -> RalphStatus | None:
    """Read `path` and parse the first RALPH_STATUS block found."""
    try:
        text = path.read_text(errors="replace")
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.warning("Could not read %s for RALPH_STATUS: %s", path, e)
        return None
    return parse_ralph_status(text)


def find_latest_ralph_status(log_dir: Path) -> tuple[Path, RalphStatus] | None:
    """Scan a directory of Claude output logs and return the most recent
    file containing a RALPH_STATUS block, plus its parsed contents.

    Used by the daemon when a worker exited without writing an outcome
    file — we fall back to inspecting its logs for a RALPH_STATUS block
    Claude may have emitted before the worker process died.
    """
    if not log_dir.exists():
        return None

    candidates = sorted(
        log_dir.glob("claude_output_*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        parsed = parse_ralph_status_from_file(path)
        if parsed:
            return (path, parsed)
    return None
