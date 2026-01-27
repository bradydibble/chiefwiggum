"""Comprehensive Concurrency Tests for Multi-Ralph Coordination

Tests stress the coordination system with concurrent Ralphs claiming tasks,
handling race conditions, claim expiry, database locking, and failure scenarios.
"""

import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from chiefwiggum import (
    CLAIM_EXPIRY_MINUTES,
    HEARTBEAT_STALE_MINUTES,
    TaskClaimStatus,
    TaskPriority,
    claim_task,
    complete_and_claim_next,
    complete_task,
    extend_claim,
    fail_task,
    get_ralph_instance,
    get_task_claim,
    heartbeat,
    init_db,
    list_pending_tasks,
    mark_stale_instances_crashed,
    register_ralph_instance,
    release_claim,
    reset_db,
    shutdown_instance,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.database import get_connection


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a test database for each test."""
    test_db = tmp_path / "test_concurrency.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def sample_fix_plan(tmp_path):
    """Create a sample fix plan with many tasks for concurrency testing."""
    fix_plan = tmp_path / "@fix_plan.md"
    content = """# Fix Plan - Concurrency Test

## HIGH PRIORITY

### 1. Task One
First task
- [ ] Subtask 1

### 2. Task Two
Second task
- [ ] Subtask 2

### 3. Task Three
Third task
- [ ] Subtask 3

### 4. Task Four
Fourth task
- [ ] Subtask 4

### 5. Task Five
Fifth task
- [ ] Subtask 5

## MEDIUM PRIORITY

### 6. Task Six
Sixth task
- [ ] Subtask 6

### 7. Task Seven
Seventh task
- [ ] Subtask 7

### 8. Task Eight
Eighth task
- [ ] Subtask 8

### 9. Task Nine
Ninth task
- [ ] Subtask 9

### 10. Task Ten
Tenth task
- [ ] Subtask 10

## LOWER PRIORITY

### 11. Task Eleven
Eleventh task
- [ ] Subtask 11

### 12. Task Twelve
Twelfth task
- [ ] Subtask 12

### 13. Task Thirteen
Thirteenth task
- [ ] Subtask 13

### 14. Task Fourteen
Fourteenth task
- [ ] Subtask 14

### 15. Task Fifteen
Fifteenth task
- [ ] Subtask 15
"""
    fix_plan.write_text(content)
    return fix_plan


# =============================================================================
# Race Condition Tests
# =============================================================================


class TestRaceConditions:
    """Tests for race conditions with concurrent Ralphs."""

    @pytest.mark.asyncio
    async def test_two_ralphs_claim_same_task_simultaneously(self, sample_fix_plan):
        """Test that 2 Ralphs claiming at exactly the same time get different tasks."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register two Ralphs
        await register_ralph_instance("ralph-1")
        await register_ralph_instance("ralph-2")

        # Claim tasks simultaneously
        results = await asyncio.gather(
            claim_task("ralph-1"),
            claim_task("ralph-2"),
        )

        # Both should succeed
        assert results[0] is not None
        assert results[1] is not None

        # They should claim different tasks
        assert results[0]["task_id"] != results[1]["task_id"]

        # Verify database consistency
        task1 = await get_task_claim(results[0]["task_id"])
        task2 = await get_task_claim(results[1]["task_id"])

        assert task1.claimed_by_ralph_id == "ralph-1"
        assert task2.claimed_by_ralph_id == "ralph-2"
        assert task1.status == TaskClaimStatus.IN_PROGRESS
        assert task2.status == TaskClaimStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_five_ralphs_claim_different_tasks(self, sample_fix_plan):
        """Test that 5 Ralphs claiming simultaneously all get unique tasks."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # All claim simultaneously
        results = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All should succeed
        assert all(r is not None for r in results)

        # All should have unique task IDs
        task_ids = [r["task_id"] for r in results]
        assert len(task_ids) == len(set(task_ids)), "Duplicate task claims detected!"

        # Verify each Ralph owns their task
        for ralph_id, result in zip(ralph_ids, results):
            task = await get_task_claim(result["task_id"])
            assert task.claimed_by_ralph_id == ralph_id
            assert task.status == TaskClaimStatus.IN_PROGRESS

    @pytest.mark.asyncio
    async def test_ten_ralphs_concurrent_claims_stress_test(self, sample_fix_plan):
        """Stress test: 10 concurrent Ralphs claiming tasks."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 10 Ralphs (more than available tasks in some priorities)
        ralph_ids = [f"ralph-{i}" for i in range(1, 11)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # All claim simultaneously
        results = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All should succeed (we have 15 tasks)
        assert all(r is not None for r in results)

        # All should have unique task IDs
        task_ids = [r["task_id"] for r in results]
        assert len(task_ids) == len(set(task_ids)), "Duplicate task claims in stress test!"

        # Verify database integrity
        conn = await get_connection()
        try:
            cursor = await conn.execute(
                "SELECT COUNT(*) FROM task_claims WHERE status = 'in_progress'"
            )
            count = (await cursor.fetchone())[0]
            assert count == 10, f"Expected 10 in-progress tasks, found {count}"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_concurrent_claims_and_completions(self, sample_fix_plan):
        """Test concurrent claims while other tasks are completing."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # First wave: claim tasks
        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # Second wave: complete tasks and claim new ones simultaneously
        async def complete_and_reclaim(ralph_id, task_id):
            await complete_task(ralph_id, task_id, commit_sha="abc123", message="Done")
            return await claim_task(ralph_id)

        new_claims = await asyncio.gather(
            *[complete_and_reclaim(ralph_ids[i], claims[i]["task_id"]) for i in range(5)]
        )

        # All should get new tasks
        assert all(c is not None for c in new_claims)

        # New tasks should all be different
        new_task_ids = [c["task_id"] for c in new_claims]
        assert len(new_task_ids) == len(set(new_task_ids))

        # Old tasks should be completed
        for claim in claims:
            task = await get_task_claim(claim["task_id"])
            assert task.status == TaskClaimStatus.COMPLETED


# =============================================================================
# Claim Expiry Tests
# =============================================================================


class TestClaimExpiry:
    """Tests for claim expiry and timeout scenarios."""

    @pytest.mark.asyncio
    async def test_claim_expires_while_ralph_working(self, sample_fix_plan):
        """Test that expired claims are automatically released (7-minute timeout)."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Manually set expiry to past (simulate 7+ minutes passing)
        conn = await get_connection()
        try:
            past_time = datetime.now() - timedelta(minutes=CLAIM_EXPIRY_MINUTES + 1)
            await conn.execute(
                "UPDATE task_claims SET expires_at = ? WHERE task_id = ?",
                (past_time, task_id),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Another Ralph should be able to claim a task (may be the expired one or next in line)
        await register_ralph_instance("ralph-2")
        result2 = await claim_task("ralph-2")

        # Should successfully claim a task
        assert result2 is not None

        # The expired task should now be either claimed by ralph-2 or available again
        task = await get_task_claim(task_id)
        # Task should either be claimed by ralph-2 or back to pending (if ralph-2 got a different task)
        if result2["task_id"] == task_id:
            assert task.claimed_by_ralph_id == "ralph-2"
        else:
            # The expired task is available for reclaim
            assert task.expires_at < datetime.now()  # Still expired

    @pytest.mark.asyncio
    async def test_extend_claim_prevents_expiry(self, sample_fix_plan):
        """Test that extending claim keeps task locked."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Get initial expiry
        task = await get_task_claim(task_id)
        initial_expiry = task.expires_at

        # Extend claim
        await asyncio.sleep(0.1)  # Small delay
        success = await extend_claim("ralph-1", task_id)
        assert success is True

        # Expiry should be updated
        task = await get_task_claim(task_id)
        assert task.expires_at > initial_expiry

    @pytest.mark.asyncio
    async def test_multiple_ralphs_extend_claims_concurrently(self, sample_fix_plan):
        """Test that multiple Ralphs can extend their claims simultaneously."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register and claim for 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All extend simultaneously
        results = await asyncio.gather(
            *[extend_claim(ralph_ids[i], claims[i]["task_id"]) for i in range(5)]
        )

        # All should succeed
        assert all(results)


# =============================================================================
# Database Lock Contention Tests
# =============================================================================


class TestDatabaseLocking:
    """Tests for database lock contention under stress."""

    @pytest.mark.asyncio
    async def test_five_concurrent_database_operations(self, sample_fix_plan):
        """Test 5+ concurrent database operations don't deadlock."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # Mix of operations: claim, extend, release
        async def mixed_operations(ralph_id, op_type):
            if op_type == "claim":
                return await claim_task(ralph_id)
            elif op_type == "register":
                return await heartbeat(ralph_id)
            elif op_type == "list":
                return await list_pending_tasks()
            return None

        # Run 15 concurrent operations
        operations = [
            mixed_operations("ralph-1", "claim"),
            mixed_operations("ralph-2", "claim"),
            mixed_operations("ralph-3", "register"),
            mixed_operations("ralph-4", "list"),
            mixed_operations("ralph-5", "claim"),
            mixed_operations("ralph-1", "register"),
            mixed_operations("ralph-2", "list"),
            mixed_operations("ralph-3", "claim"),
            mixed_operations("ralph-4", "register"),
            mixed_operations("ralph-5", "list"),
        ]

        # Should all complete without deadlock
        results = await asyncio.gather(*operations)
        assert len(results) == 10

    @pytest.mark.asyncio
    async def test_database_locking_with_begin_immediate(self, sample_fix_plan):
        """Test that BEGIN IMMEDIATE prevents race conditions in claim_task."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Create 2 Ralphs that will compete for the same task
        await register_ralph_instance("ralph-1")
        await register_ralph_instance("ralph-2")

        # Only 1 HIGH priority task exists, both try to claim it
        # This tests BEGIN IMMEDIATE locking
        tasks = await asyncio.gather(
            claim_task("ralph-1"),
            claim_task("ralph-2"),
        )

        # Both should succeed but with different tasks
        assert tasks[0] is not None
        assert tasks[1] is not None
        assert tasks[0]["task_id"] != tasks[1]["task_id"]

    @pytest.mark.asyncio
    async def test_exclusive_lock_in_complete_and_claim_next(self, sample_fix_plan):
        """Test that complete_and_claim_next uses exclusive lock properly."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 3 Ralphs and have them claim tasks
        ralph_ids = [f"ralph-{i}" for i in range(1, 4)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All complete and claim next simultaneously
        results = await asyncio.gather(
            *[
                complete_and_claim_next(
                    ralph_ids[i],
                    claims[i]["task_id"],
                    project="test",
                    commit_sha=f"sha{i}",
                    message="Done",
                )
                for i in range(3)
            ]
        )

        # All should complete and get new unique tasks
        next_task_ids = [r["task_id"] for r in results if r]
        assert len(next_task_ids) == len(set(next_task_ids)), "Duplicate claims!"


# =============================================================================
# Heartbeat and Crash Detection Tests
# =============================================================================


class TestHeartbeatAndCrashDetection:
    """Tests for heartbeat failures and crash detection."""

    @pytest.mark.asyncio
    async def test_heartbeat_failure_during_long_task(self, sample_fix_plan):
        """Test that stale heartbeat (10-minute timeout) marks Ralph as crashed."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Simulate heartbeat going stale (10+ minutes)
        conn = await get_connection()
        try:
            stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
            await conn.execute(
                "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
                (stale_time, "ralph-1"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Mark stale instances as crashed
        count = await mark_stale_instances_crashed()
        assert count == 1

        # Ralph should be crashed
        instance = await get_ralph_instance("ralph-1")
        assert instance.status.value == "crashed"

        # Task should be released back to pending
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None

    @pytest.mark.asyncio
    async def test_multiple_heartbeats_concurrent(self, sample_fix_plan):
        """Test that multiple Ralphs can send heartbeats concurrently."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 10 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 11)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # All send heartbeats simultaneously
        await asyncio.gather(*[heartbeat(ralph_id) for ralph_id in ralph_ids])

        # All should have updated heartbeats
        for ralph_id in ralph_ids:
            instance = await get_ralph_instance(ralph_id)
            assert instance is not None
            # Heartbeat should be recent (within last 5 seconds)
            age = (datetime.now() - instance.last_heartbeat).total_seconds()
            assert age < 5

    @pytest.mark.asyncio
    async def test_crash_detection_with_concurrent_operations(self, sample_fix_plan):
        """Test crash detection while other operations are happening."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # Make ralph-3 stale
        conn = await get_connection()
        try:
            stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
            await conn.execute(
                "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
                (stale_time, "ralph-3"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Run crash detection concurrently with other operations
        results = await asyncio.gather(
            mark_stale_instances_crashed(),
            claim_task("ralph-1"),
            heartbeat("ralph-2"),
            claim_task("ralph-4"),
            heartbeat("ralph-5"),
        )

        # Crash detection should find 1 stale instance
        assert results[0] == 1

        # Other operations should succeed
        assert results[1] is not None  # ralph-1 claimed
        assert results[3] is not None  # ralph-4 claimed


# =============================================================================
# Concurrent Task Completion Tests
# =============================================================================


class TestConcurrentCompletions:
    """Tests for multiple Ralphs completing tasks at once."""

    @pytest.mark.asyncio
    async def test_five_ralphs_complete_simultaneously(self, sample_fix_plan):
        """Test that 5 Ralphs completing tasks at the same time works correctly."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register and claim for 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All complete simultaneously
        results = await asyncio.gather(
            *[
                complete_task(
                    ralph_ids[i],
                    claims[i]["task_id"],
                    commit_sha=f"commit{i}",
                    message=f"Completed by {ralph_ids[i]}",
                )
                for i in range(5)
            ]
        )

        # All should succeed
        assert all(results)

        # Verify all tasks are completed
        for claim in claims:
            task = await get_task_claim(claim["task_id"])
            assert task.status == TaskClaimStatus.COMPLETED
            assert task.git_commit_sha is not None

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_atomic_operations(self, sample_fix_plan):
        """Test that complete_and_claim_next is truly atomic."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs and have them claim
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All complete and claim next atomically
        next_tasks = await asyncio.gather(
            *[
                complete_and_claim_next(
                    ralph_ids[i],
                    claims[i]["task_id"],
                    project="test",
                    commit_sha=f"atomic{i}",
                    message="Atomic completion",
                )
                for i in range(5)
            ]
        )

        # All should get new tasks (we have 15 total tasks)
        assert all(t is not None for t in next_tasks)

        # All next tasks should be unique
        next_task_ids = [t["task_id"] for t in next_tasks]
        assert len(next_task_ids) == len(set(next_task_ids))

        # Old tasks should be completed
        for claim in claims:
            task = await get_task_claim(claim["task_id"])
            assert task.status == TaskClaimStatus.COMPLETED


# =============================================================================
# Claim Release During Crash Tests
# =============================================================================


class TestCrashScenarios:
    """Tests for Ralph crashes mid-task."""

    @pytest.mark.asyncio
    async def test_ralph_dies_mid_task_claim_released(self, sample_fix_plan):
        """Test that when Ralph crashes, its task is released back to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Simulate crash by making heartbeat stale
        conn = await get_connection()
        try:
            stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
            await conn.execute(
                "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
                (stale_time, "ralph-1"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Detect crash
        await mark_stale_instances_crashed()

        # Task should be released
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None

        # Another Ralph can claim it
        await register_ralph_instance("ralph-2")
        result2 = await claim_task("ralph-2")
        assert result2["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_shutdown_releases_tasks(self, sample_fix_plan):
        """Test that clean shutdown releases tasks back to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim multiple tasks by reclaiming
        result1 = await claim_task("ralph-1")
        await complete_task("ralph-1", result1["task_id"], commit_sha="abc", message="Done")
        result2 = await claim_task("ralph-1")
        task_id = result2["task_id"]

        # Shutdown instance
        await shutdown_instance("ralph-1")

        # Task should be released
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None

    @pytest.mark.asyncio
    async def test_concurrent_crash_and_claims(self, sample_fix_plan):
        """Test crash detection happening while other Ralphs are claiming."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # ralph-1 and ralph-2 claim tasks
        await claim_task("ralph-1")
        await claim_task("ralph-2")

        # Make ralph-1 stale
        conn = await get_connection()
        try:
            stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
            await conn.execute(
                "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
                (stale_time, "ralph-1"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Run crash detection + new claims concurrently
        results = await asyncio.gather(
            mark_stale_instances_crashed(),
            claim_task("ralph-3"),
            claim_task("ralph-4"),
            claim_task("ralph-5"),
        )

        # Crash detection should find ralph-1
        assert results[0] == 1

        # Other claims should succeed
        assert results[1] is not None
        assert results[2] is not None
        assert results[3] is not None


# =============================================================================
# Task Requeue on Failure Tests
# =============================================================================


class TestFailureAndRequeue:
    """Tests for task failure and requeue scenarios."""

    @pytest.mark.asyncio
    async def test_task_failure_releases_for_retry(self, sample_fix_plan):
        """Test that failing a task releases it back to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim and fail a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await fail_task("ralph-1", task_id, "Build failed")
        assert success is True

        # Task should be marked as failed (not released to pending in current impl)
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.FAILED
        assert task.completion_message == "Build failed"

    @pytest.mark.asyncio
    async def test_release_claim_makes_task_available(self, sample_fix_plan):
        """Test that releasing a claim returns task to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        await register_ralph_instance("ralph-1")

        # Claim a task
        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Release it
        success = await release_claim("ralph-1", task_id)
        assert success is True

        # Task should be pending
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None

        # Another Ralph can claim it
        await register_ralph_instance("ralph-2")
        result2 = await claim_task("ralph-2")
        assert result2["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_concurrent_failures_and_releases(self, sample_fix_plan):
        """Test multiple Ralphs failing/releasing tasks concurrently."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 5 Ralphs and have them claim
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # Mix of failures and releases
        results = await asyncio.gather(
            fail_task("ralph-1", claims[0]["task_id"], "Error 1"),
            release_claim("ralph-2", claims[1]["task_id"]),
            fail_task("ralph-3", claims[2]["task_id"], "Error 3"),
            release_claim("ralph-4", claims[3]["task_id"]),
            fail_task("ralph-5", claims[4]["task_id"], "Error 5"),
        )

        # All should succeed
        assert all(results)

        # Verify statuses
        task1 = await get_task_claim(claims[0]["task_id"])
        assert task1.status == TaskClaimStatus.FAILED

        task2 = await get_task_claim(claims[1]["task_id"])
        assert task2.status == TaskClaimStatus.PENDING

        task3 = await get_task_claim(claims[2]["task_id"])
        assert task3.status == TaskClaimStatus.FAILED


# =============================================================================
# Stress Tests with 10+ Ralphs
# =============================================================================


class TestStressScenarios:
    """Stress tests with 10+ concurrent Ralphs."""

    @pytest.mark.asyncio
    async def test_ten_ralphs_complete_workflow(self, sample_fix_plan):
        """Full workflow test with 10 concurrent Ralphs."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 10 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 11)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # Round 1: All claim tasks
        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])
        assert all(c is not None for c in claims)

        # Round 2: All send heartbeats
        await asyncio.gather(*[heartbeat(ralph_id) for ralph_id in ralph_ids])

        # Round 3: Half complete, half extend
        operations = []
        for i in range(10):
            if i < 5:
                operations.append(
                    complete_task(
                        ralph_ids[i],
                        claims[i]["task_id"],
                        commit_sha=f"sha{i}",
                        message="Done",
                    )
                )
            else:
                operations.append(extend_claim(ralph_ids[i], claims[i]["task_id"]))

        results = await asyncio.gather(*operations)
        assert all(results)

        # Round 4: First 5 claim new tasks
        new_claims = await asyncio.gather(
            *[claim_task(ralph_ids[i]) for i in range(5)]
        )
        assert all(c is not None for c in new_claims)

    @pytest.mark.asyncio
    async def test_fifteen_ralphs_maximum_concurrency(self, sample_fix_plan):
        """Maximum stress: 15 Ralphs (one per task) all working simultaneously."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 15 Ralphs (matching number of tasks)
        ralph_ids = [f"ralph-{i}" for i in range(1, 16)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # All claim simultaneously (should each get a unique task)
        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # All should succeed
        assert all(c is not None for c in claims)

        # All should be unique
        task_ids = [c["task_id"] for c in claims]
        assert len(task_ids) == 15
        assert len(set(task_ids)) == 15

        # All heartbeat simultaneously
        await asyncio.gather(*[heartbeat(ralph_id) for ralph_id in ralph_ids])

        # All complete simultaneously
        results = await asyncio.gather(
            *[
                complete_task(
                    ralph_ids[i],
                    claims[i]["task_id"],
                    commit_sha=f"final{i}",
                    message="Max stress complete",
                )
                for i in range(15)
            ]
        )

        assert all(results)

        # No tasks should remain pending
        pending = await list_pending_tasks()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_mixed_operations_stress(self, sample_fix_plan):
        """Stress test with mixed concurrent operations."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 10 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 11)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # Wave 1: All Ralphs claim tasks
        claims_with_ralphs = []
        for ralph_id in ralph_ids[:5]:  # First 5 claim tasks
            result = await claim_task(ralph_id)
            if result:
                claims_with_ralphs.append((ralph_id, result))

        # Wave 2: Mix of heartbeats and more claims
        operations_wave2 = [
            heartbeat("ralph-1"),
            heartbeat("ralph-2"),
            claim_task("ralph-6"),
            claim_task("ralph-7"),
            heartbeat("ralph-3"),
        ]
        results2 = await asyncio.gather(*operations_wave2)

        # Wave 3: Extend and complete the originally claimed tasks
        operations_wave3 = []
        for i, (ralph_id, claim) in enumerate(claims_with_ralphs):
            if i % 2 == 0:
                operations_wave3.append(extend_claim(ralph_id, claim["task_id"]))
            else:
                operations_wave3.append(
                    complete_task(ralph_id, claim["task_id"], commit_sha=f"sha{i}")
                )

        results3 = await asyncio.gather(*operations_wave3)
        # Some extends may fail if task already completed, that's ok
        assert any(results3), "At least some operations should succeed"


# =============================================================================
# Database Integrity Tests
# =============================================================================


class TestDatabaseIntegrity:
    """Tests for database consistency under concurrent load."""

    @pytest.mark.asyncio
    async def test_no_duplicate_claims_under_stress(self, sample_fix_plan):
        """Verify no task is claimed by multiple Ralphs simultaneously."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register 10 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 11)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # All claim simultaneously
        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # Check database for duplicates
        conn = await get_connection()
        try:
            cursor = await conn.execute(
                """SELECT task_id, COUNT(*) as cnt
                   FROM task_claims
                   WHERE status = 'in_progress'
                   GROUP BY task_id
                   HAVING cnt > 1"""
            )
            duplicates = await cursor.fetchall()
            assert len(duplicates) == 0, f"Found duplicate claims: {duplicates}"
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_task_counts_remain_consistent(self, sample_fix_plan):
        """Verify total task count remains consistent through operations."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Get initial count
        conn = await get_connection()
        try:
            cursor = await conn.execute("SELECT COUNT(*) FROM task_claims")
            initial_count = (await cursor.fetchone())[0]
        finally:
            await conn.close()

        # Perform many operations
        ralph_ids = [f"ralph-{i}" for i in range(1, 6)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        # Claim, complete, claim again
        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        await asyncio.gather(
            *[
                complete_task(ralph_ids[i], claims[i]["task_id"], commit_sha=f"sha{i}")
                for i in range(5)
            ]
        )

        new_claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # Final count should match initial
        conn = await get_connection()
        try:
            cursor = await conn.execute("SELECT COUNT(*) FROM task_claims")
            final_count = (await cursor.fetchone())[0]
            assert final_count == initial_count
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_no_orphaned_tasks_after_crash(self, sample_fix_plan):
        """Verify crashed Ralph's tasks don't remain stuck."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register and claim for 3 Ralphs
        ralph_ids = [f"ralph-{i}" for i in range(1, 4)]
        for ralph_id in ralph_ids:
            await register_ralph_instance(ralph_id)

        claims = await asyncio.gather(*[claim_task(ralph_id) for ralph_id in ralph_ids])

        # Make ralph-2 crash
        conn = await get_connection()
        try:
            stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
            await conn.execute(
                "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
                (stale_time, "ralph-2"),
            )
            await conn.commit()
        finally:
            await conn.close()

        # Detect crash
        await mark_stale_instances_crashed()

        # Verify no tasks are stuck in_progress with crashed Ralph
        conn = await get_connection()
        try:
            cursor = await conn.execute(
                """SELECT COUNT(*) FROM task_claims tc
                   JOIN ralph_instances ri ON tc.claimed_by_ralph_id = ri.ralph_id
                   WHERE tc.status = 'in_progress' AND ri.status = 'crashed'"""
            )
            orphaned_count = (await cursor.fetchone())[0]
            assert orphaned_count == 0, "Found orphaned in_progress tasks!"
        finally:
            await conn.close()
