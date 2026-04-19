"""End-to-end: worker writes outcome.json → daemon consumes → DB is updated.

This is the new worker→daemon communication channel that replaces the old
shell-based `wig complete` / `wig release` calls. If these tests break,
the core flow (worker finishes a task → task marked complete → next
worker spawns) is broken.
"""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from chiefwiggum import (
    init_db,
    list_active_instances,
    register_ralph_instance,
    reset_db,
)
from chiefwiggum.coordination import claim_task
from chiefwiggum.daemon import DaemonStats, _process_worker_outcomes
from chiefwiggum.models import TaskPriority
from chiefwiggum.outcome import (
    WorkerExitStatus,
    WorkerOutcome,
    list_pending_outcomes,
    write_outcome,
)
from chiefwiggum.paths import reset_paths_cache


@pytest.fixture(autouse=True)
async def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEFWIGGUM_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_paths_cache()
    await init_db()
    yield tmp_path
    await reset_db()
    reset_paths_cache()


async def _seed_and_claim(project: str, ralph_id: str, task_id: str, title: str) -> None:
    from chiefwiggum.database import get_connection
    conn = await get_connection()
    try:
        await conn.execute(
            """INSERT INTO task_claims
                 (task_id, task_title, task_priority, project, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
            (task_id, title, TaskPriority.HIGH.value, project),
        )
        await conn.commit()
    finally:
        await conn.close()
    await register_ralph_instance(ralph_id, project=project)
    claimed = await claim_task(ralph_id, project=project)
    assert claimed is not None, f"fixture claim should succeed for {task_id}"


async def _task_status(task_id: str) -> str:
    from chiefwiggum.database import get_connection
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT status FROM task_claims WHERE task_id = ?", (task_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else "<missing>"
    finally:
        await conn.close()


class TestOutcomeConsumption:
    @pytest.mark.asyncio
    async def test_success_outcome_marks_task_completed(self):
        await _seed_and_claim("demo", "ralph-a", "task-X", "The X task")

        write_outcome(WorkerOutcome(
            ralph_id="ralph-a",
            status=WorkerExitStatus.SUCCESS,
            task_id="task-X",
            commit_sha="abc1234",
        ))

        stats = DaemonStats()
        await _process_worker_outcomes(stats)

        assert await _task_status("task-X") == "completed"
        assert stats.completions_recorded == 1
        assert stats.outcomes_consumed == 1
        # outcome.json should be gone.
        assert list_pending_outcomes() == []
        # ralph_instance should be shutdown, not active.
        active = await list_active_instances()
        assert all(inst.ralph_id != "ralph-a" for inst in active)

    @pytest.mark.asyncio
    async def test_failed_outcome_marks_task_failed(self):
        await _seed_and_claim("demo", "ralph-b", "task-Y", "The Y task")
        write_outcome(WorkerOutcome(
            ralph_id="ralph-b",
            status=WorkerExitStatus.FAILED,
            task_id="task-Y",
            error_category="code_error",
            error_message="pytest red",
        ))

        stats = DaemonStats()
        await _process_worker_outcomes(stats)

        # fail_task moves it to 'retry_pending' or 'failed' depending on
        # retry budget. Either is fine — it's OUT of 'in_progress'.
        final = await _task_status("task-Y")
        assert final in {"failed", "retry_pending"}
        assert stats.failures_recorded == 1
        assert list_pending_outcomes() == []

    @pytest.mark.asyncio
    async def test_crashed_outcome_releases_claim_so_next_worker_can_retry(self):
        await _seed_and_claim("demo", "ralph-c", "task-Z", "The Z task")

        write_outcome(WorkerOutcome(
            ralph_id="ralph-c",
            status=WorkerExitStatus.CRASHED,
            task_id="task-Z",
            error_message="bash: unbound variable",
        ))

        stats = DaemonStats()
        await _process_worker_outcomes(stats)

        # Claim was released; task is back in 'pending' (or 'released').
        status = await _task_status("task-Z")
        assert status in {"pending", "released"}, (
            f"crashed worker's claim must be released; got status={status}"
        )
        assert stats.releases_recorded == 1

    @pytest.mark.asyncio
    async def test_complete_is_idempotent_across_duplicate_outcomes(self):
        """If the daemon crashes mid-consume and re-reads the same outcome
        file later (e.g. we haven't deleted it yet), the second
        complete_task call must return True without corrupting state."""
        await _seed_and_claim("demo", "ralph-d", "task-D", "The D task")

        # Process once.
        write_outcome(WorkerOutcome(
            ralph_id="ralph-d",
            status=WorkerExitStatus.SUCCESS,
            task_id="task-D",
            commit_sha="deadbeef",
        ))
        stats1 = DaemonStats()
        await _process_worker_outcomes(stats1)
        assert await _task_status("task-D") == "completed"

        # Re-write the same outcome and process again — simulates restart
        # mid-consume. No errors, idempotent result.
        write_outcome(WorkerOutcome(
            ralph_id="ralph-d",
            status=WorkerExitStatus.SUCCESS,
            task_id="task-D",
            commit_sha="deadbeef",
        ))
        stats2 = DaemonStats()
        await _process_worker_outcomes(stats2)
        assert await _task_status("task-D") == "completed"
        assert stats2.completions_recorded == 1

    @pytest.mark.asyncio
    async def test_malformed_outcome_is_dropped_not_blocking(self):
        """A corrupt outcome file must not jam the reconcile loop —
        delete it and move on."""
        from chiefwiggum.outcome import get_outcome_path
        p = get_outcome_path("ralph-bogus")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("this is not valid JSON {{{")

        stats = DaemonStats()
        await _process_worker_outcomes(stats)

        assert list_pending_outcomes() == []

    @pytest.mark.asyncio
    async def test_empty_outcome_queue_is_a_noop(self):
        stats = DaemonStats()
        await _process_worker_outcomes(stats)
        assert stats.outcomes_consumed == 0
