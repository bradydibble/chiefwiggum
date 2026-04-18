"""End-to-end flow test covering the user's mental model:

  CHIEFWIGGUM (orchestrator)
      └── PROJECT (workspace, e.g. `tian`)
             └── RALPH WORKER (long-lived process; claims one task at a time)
                    └── RALPH LOOP (throwaway Claude session, one per task)

The worker self-chains tasks internally: reset the Claude session, claim
the next task, run a fresh session, repeat until the queue drains.
The daemon's job is to (1) start workers when the user asks, and
(2) respawn a worker that CRASHED mid-work — it does NOT micromanage
task-to-task progression inside a healthy worker.

This test asserts both roles.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from chiefwiggum import (
    complete_task,
    init_db,
    register_ralph_instance,
    reset_db,
    shutdown_instance,
)
from chiefwiggum.coordination import (
    claim_task,
    enqueue_spawn_request,
    fetch_pending_spawn_requests,
    mark_stale_instances_crashed,
    projects_needing_ralphs,
)
from chiefwiggum.daemon import (
    DaemonStats,
    _autospawn_for_unattended_projects,
    _tick,
)
from chiefwiggum.models import RalphInstanceStatus, TaskPriority
from chiefwiggum.paths import reset_paths_cache


@pytest.fixture(autouse=True)
async def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEFWIGGUM_DB", str(tmp_path / "autospawn.db"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    reset_paths_cache()
    await init_db()
    project_dir = tmp_path / "claudecode" / "demo"
    project_dir.mkdir(parents=True)
    (project_dir / "@fix_plan.md").write_text("# Demo\n")
    yield tmp_path
    await reset_db()
    reset_paths_cache()


async def _seed_pending_tasks(n: int, project: str = "demo") -> list[str]:
    from chiefwiggum.database import get_connection
    ids: list[str] = []
    conn = await get_connection()
    try:
        for i in range(n):
            task_id = f"task-{i + 1}"
            ids.append(task_id)
            await conn.execute(
                """INSERT INTO task_claims
                     (task_id, task_title, task_priority, project, status, created_at)
                   VALUES (?, ?, ?, ?, 'pending', CURRENT_TIMESTAMP)""",
                (task_id, f"task {i + 1}", TaskPriority.HIGH.value, project),
            )
        await conn.commit()
    finally:
        await conn.close()
    return ids


async def _tasks_by_status(status: str) -> list[str]:
    from chiefwiggum.database import get_connection
    conn = await get_connection()
    try:
        cursor = await conn.execute(
            "SELECT task_id FROM task_claims WHERE status = ? ORDER BY task_id",
            (status,),
        )
        return [row[0] for row in await cursor.fetchall()]
    finally:
        await conn.close()


class TestExplicitSpawnThenWorkerChains:
    """User's primary flow:
       1. `wig spawn demo` → writes a spawn_request.
       2. Daemon consumes it → spawns worker W1.
       3. W1 claims task-1, runs a Claude session ('ralph loop') to complete
          it, resets the session, claims task-2, runs another session,
          completes task-2. Queue drains. W1 exits.
       4. Daemon does NOT re-spawn: nothing's pending, nothing's unattended.
    """

    @pytest.mark.asyncio
    async def test_spawn_then_self_chain_to_empty_queue(self):
        await _seed_pending_tasks(2)

        spawned_workers: list[str] = []

        async def fake_spawn(**kwargs):
            # Simulate the spawner's CLAIM-AND-SPAWN behavior: register the
            # worker and claim its first task.
            ralph_id = kwargs["ralph_id"]
            project = kwargs["project"]
            spawned_workers.append(ralph_id)
            await register_ralph_instance(ralph_id, project=project)
            claim = await claim_task(ralph_id, project=project)
            return (True, f"spawned {ralph_id}", claim["task_id"] if claim else None)

        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim",
                   new=AsyncMock(side_effect=fake_spawn)), \
             patch("chiefwiggum.daemon.cleanup_dead_ralphs", return_value=[]):
            stats = DaemonStats()

            # 1. User issues explicit spawn intent.
            await enqueue_spawn_request(project_path="demo", requested_by="cli")

            # 2. Daemon tick consumes the intent → W1 spawned on task-1.
            await _tick(stats)
            assert len(spawned_workers) == 1
            w1 = spawned_workers[0]
            assert await _tasks_by_status("in_progress") == ["task-1"]

            # 3. The WORKER itself chains tasks. The daemon is not involved.
            # Simulate W1 finishing task-1 and self-chaining to task-2.
            await complete_task(w1, "task-1")
            task2 = await claim_task(w1, project="demo")
            assert task2["task_id"] == "task-2"

            # 4. While W1 is active (claimed task-2), the daemon's autospawn
            # MUST NOT fire — we don't want to double-spawn workers.
            await _autospawn_for_unattended_projects(stats)
            extra_requests = [r for r in await fetch_pending_spawn_requests()
                              if r["requested_by"] == "daemon-autospawn"]
            assert extra_requests == []
            assert len(spawned_workers) == 1, "worker already active; no extra spawn"

            # 5. W1 finishes task-2, queue drained, W1 exits naturally.
            await complete_task(w1, "task-2")
            await shutdown_instance(w1)
            assert await _tasks_by_status("pending") == []

            # 6. Next tick — nothing to do. No auto-respawn because the queue
            # is empty.
            await _tick(stats)
            assert len(spawned_workers) == 1, "queue drained; daemon should not spawn again"


class TestRespawnOnCrash:
    """User's fault-tolerance expectation: if the worker crashes mid-work
    (OOM, bash error, laptop woke up wedged), the daemon respawns a fresh
    worker so work continues. The user only had to click spawn once."""

    @pytest.mark.asyncio
    async def test_crashed_worker_gets_respawned_by_daemon(self):
        await _seed_pending_tasks(3)

        spawned: list[str] = []

        async def fake_spawn(**kwargs):
            ralph_id = kwargs["ralph_id"]
            project = kwargs["project"]
            spawned.append(ralph_id)
            await register_ralph_instance(ralph_id, project=project)
            await claim_task(ralph_id, project=project)
            return (True, "ok", None)

        with patch("chiefwiggum.daemon.spawn_ralph_with_task_claim",
                   new=AsyncMock(side_effect=fake_spawn)), \
             patch("chiefwiggum.daemon.cleanup_dead_ralphs", return_value=[]):
            stats = DaemonStats()

            # Explicit spawn then one tick spawns W1.
            await enqueue_spawn_request(project_path="demo", requested_by="cli")
            await _tick(stats)
            assert len(spawned) == 1
            w1 = spawned[0]

            # Simulate crash: W1's heartbeat goes stale, mark it crashed,
            # which should release its claim and make the project "unattended
            # but user-requested" again.
            from chiefwiggum.database import get_connection
            conn = await get_connection()
            try:
                await conn.execute(
                    """UPDATE ralph_instances
                          SET status = 'crashed',
                              last_heartbeat = datetime('now', '-30 minutes')
                        WHERE ralph_id = ?""",
                    (w1,),
                )
                # Release the in-progress claim so it's pending again
                await conn.execute(
                    """UPDATE task_claims SET status='pending', claimed_by_ralph_id=NULL
                        WHERE claimed_by_ralph_id = ?""",
                    (w1,),
                )
                await conn.commit()
            finally:
                await conn.close()

            # mark_stale_instances_crashed is a no-op here (already marked),
            # but running it keeps the tick realistic.
            await mark_stale_instances_crashed()

            # Project is now in the autospawn candidate list because
            # spawn_requests EXISTS for it and there's pending work with no
            # active worker.
            assert await projects_needing_ralphs() == ["demo"]

            # Next tick → daemon autospawns W2.
            await _tick(stats)
            assert len(spawned) == 2
            assert spawned[0] != spawned[1], "W2 must be a fresh ralph_id"


class TestAutospawnDoesNotRunawayUnrequestedProjects:
    """Regression guard: a project with pending tasks in the DB from a
    previous session should NOT get auto-spawned if the user hasn't done
    `wig spawn` for it in this session. Otherwise booting the daemon
    silently starts billable work."""

    @pytest.mark.asyncio
    async def test_pending_tasks_without_spawn_request_are_ignored(self):
        await _seed_pending_tasks(2)  # tasks exist
        # ...but NO spawn_request row.
        candidates = await projects_needing_ralphs()
        assert candidates == [], (
            "autospawn fired on a project the user never asked to run — "
            "this is the 'runaway billable work' bug"
        )


class TestAutospawnDeDupePendingRequests:
    """Regression guard: the autospawn step must not enqueue a second
    spawn_request while an earlier one is still waiting to be consumed."""

    @pytest.mark.asyncio
    async def test_no_duplicate_autospawn_while_previous_is_pending(self):
        await _seed_pending_tasks(1)
        await enqueue_spawn_request(project_path="demo", requested_by="cli")

        stats = DaemonStats()
        await _autospawn_for_unattended_projects(stats)
        await _autospawn_for_unattended_projects(stats)

        # The autospawn guard should have skipped because a pending
        # spawn_request already exists for "demo". The only row is the
        # original "cli" request; no "daemon-autospawn" duplicate.
        pending = await fetch_pending_spawn_requests()
        autospawn_rows = [r for r in pending if r["requested_by"] == "daemon-autospawn"]
        assert autospawn_rows == []
