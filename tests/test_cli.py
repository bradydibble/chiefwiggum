"""Tests for ChiefWiggum CLI commands."""

import os
import re
import socket

import pytest
from click.testing import CliRunner

from chiefwiggum import init_db, reset_db
from chiefwiggum.cli import generate_ralph_id, main


def _parse_task_id(output: str) -> str | None:
    """Extract task ID from CLI output (strips any ANSI escape codes)."""
    clean = re.sub(r"\x1b\[[0-9;]*m", "", output)
    for line in clean.splitlines():
        if "Task ID:" in line:
            return line.split("Task ID:")[-1].strip()
    return None


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture(autouse=True)
async def cli_db(tmp_path):
    """Isolated test database for each CLI test."""
    os.environ["CHIEFWIGGUM_DB"] = str(tmp_path / "test_cli.db")
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def fix_plan_path(tmp_path):
    """A minimal @fix_plan.md with parseable tasks."""
    plan = tmp_path / "fix_plan.md"
    plan.write_text(
        """# Test Project Tasks

## HIGH PRIORITY

### 1. Implement feature A
Core feature.
- [ ] Write the code
- [ ] Write tests

### 2. Fix bug B
Important fix.
- [ ] Identify root cause
- [ ] Apply fix
"""
    )
    return plan


def test_init_command(runner):
    result = runner.invoke(main, ["init"])
    assert result.exit_code == 0


def test_status_empty_db(runner):
    result = runner.invoke(main, ["status"])
    assert result.exit_code == 0


def test_sync_then_list(runner, fix_plan_path):
    result = runner.invoke(main, ["sync", str(fix_plan_path), "--project", "testproject"])
    assert result.exit_code == 0
    assert "Synced" in result.output

    result = runner.invoke(main, ["list", "--all"])
    assert result.exit_code == 0


def test_claim_and_complete_flow(runner, fix_plan_path):
    # Sync tasks
    result = runner.invoke(main, ["sync", str(fix_plan_path), "--project", "testproject"])
    assert result.exit_code == 0

    # Claim
    result = runner.invoke(main, ["claim", "testproject", "--ralph-id", "test-ralph"])
    assert result.exit_code == 0
    assert "Claimed" in result.output

    task_id = _parse_task_id(result.output)
    assert task_id is not None

    # Complete
    result = runner.invoke(main, ["complete", task_id, "--ralph-id", "test-ralph"])
    assert result.exit_code == 0


def test_complete_exits_nonzero_when_task_not_claimed(runner, fix_plan_path):
    """Regression: `wig complete` must exit 1 (not 0) when the task isn't
    claimed by this Ralph — otherwise the worker's shell `$?` check thinks
    completion succeeded and self-chains on a task that was never marked
    done, corrupting queue state. Caused a real outage yesterday."""
    # Sync tasks but DON'T claim — the complete call should fail.
    runner.invoke(main, ["sync", str(fix_plan_path), "--project", "testproject"])

    result = runner.invoke(
        main,
        ["complete", "some-fake-task-id", "--ralph-id", "nobody"],
    )
    assert result.exit_code != 0, (
        "wig complete should return non-zero when task_id isn't claimed by "
        f"the given ralph_id; output was:\n{result.output}"
    )
    assert "Failed to complete" in result.output


def test_release_command(runner, fix_plan_path):
    # Sync and claim
    runner.invoke(main, ["sync", str(fix_plan_path), "--project", "testproject"])
    result = runner.invoke(main, ["claim", "testproject", "--ralph-id", "test-ralph"])
    assert result.exit_code == 0

    task_id = _parse_task_id(result.output)
    assert task_id is not None

    # Release
    result = runner.invoke(main, ["release", task_id, "--ralph-id", "test-ralph"])
    assert result.exit_code == 0
    assert "Released" in result.output


def test_export_history_command(runner, tmp_path):
    output_path = str(tmp_path / "output.csv")
    result = runner.invoke(main, ["export-history", output_path])
    assert result.exit_code == 0


def test_generate_ralph_id_format():
    ralph_id = generate_ralph_id()
    hostname = socket.gethostname().split(".")[0]
    assert hostname in ralph_id


def test_generate_ralph_id_with_name():
    ralph_id = generate_ralph_id("worker")
    hostname = socket.gethostname().split(".")[0]
    assert ralph_id == f"{hostname}-worker"
