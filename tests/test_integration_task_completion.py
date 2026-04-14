"""End-to-end integration tests for task completion workflow.

These tests verify the complete flow: spawn → claim → work → complete → next task.
"""

import os

import pytest

from chiefwiggum import (
    claim_task,
    complete_and_claim_next,
    get_ralph_instance,
    get_task_claim,
    init_db,
    register_ralph_instance,
    reset_db,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.database import get_connection
from chiefwiggum.fix_plan_writer import (
    check_task_marked_complete,
    update_task_completion_marker,
)
from chiefwiggum.spawner import (
    check_task_completion,
    get_ralph_log_path,
)


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a test database for each test."""
    test_db = tmp_path / "test_integration.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def sample_fix_plan(tmp_path):
    """Create a sample @fix_plan.md file for testing."""
    fix_plan = tmp_path / "@fix_plan.md"
    content = """# Fix Plan

## HIGH Priority

### 1. First Task
Description of first task
- Requirement 1
- Requirement 2

### 2. Second Task
Description of second task
- Requirement A
- Requirement B

### 3. Third Task
Description of third task
- Requirement X
- Requirement Y
"""
    fix_plan.write_text(content)
    return fix_plan


@pytest.fixture
def mock_ralph_data_dir(tmp_path, monkeypatch):
    """Monkeypatch path functions to use temp directory."""
    import chiefwiggum.spawner as spawner_module
    ralph_dir = tmp_path / ".chiefwiggum" / "ralphs"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    status_dir = ralph_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(spawner_module, "_get_ralph_data_dir", lambda: ralph_dir)
    monkeypatch.setattr(spawner_module, "_get_task_prompts_dir", lambda: ralph_dir / "task_prompts")
    monkeypatch.setattr(spawner_module, "_get_status_dir", lambda: status_dir)
    return ralph_dir


class TestCompleteWorkflow:
    """Test complete workflow: spawn → claim → work → complete → next task."""

    @pytest.mark.asyncio
    async def test_complete_workflow_spawn_to_next_task(self, sample_fix_plan, mock_ralph_data_dir):
        """Test complete workflow from task claim through completion to next task."""
        # Step 1: Sync tasks to database
        synced = await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        assert synced == 3

        # Step 2: Register Ralph instance
        ralph_id = "test-ralph-e2e"
        await register_ralph_instance(ralph_id, project="test")

        # Step 3: Claim first task
        task1 = await claim_task(ralph_id, project="test")
        assert task1 is not None
        assert task1['task_id'] == "task-1-first-task"
        assert task1['task_priority'] == "HIGH"

        # Step 4: Simulate Ralph working on task (write log)
        log_path = get_ralph_log_path(ralph_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        fake_commit_sha = "abc1234567890def1234567890abcdef12345678"
        log_path.write_text(f"""
[2026-01-23 18:00:00] Ralph working on task-1
[2026-01-23 18:15:00] Running tests...
[2026-01-23 18:20:00] All tests pass!
---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: task-1-first-task
COMMIT: {fake_commit_sha}
VERIFICATION: Task 1 completed successfully
---END_RALPH_STATUS---
""")

        # Step 5: Check completion detection
        task_id, failure, commit_sha = check_task_completion(ralph_id)
        assert task_id == "task-1-first-task"
        assert failure is None
        assert commit_sha == fake_commit_sha

        # Step 6: Update fix_plan.md with checkmark
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-1-first-task",
            task_number=1,
            mark_complete=True
        )
        assert success is True

        # Step 7: Mark complete in database and claim next task
        next_task = await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha=commit_sha,
            message="Completed task 1"
        )

        # Step 8: Verify task 1 marked complete in database
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT status, git_commit_sha, completed_at FROM task_claims WHERE task_id = ?",
            ("task-1-first-task",)
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == fake_commit_sha
        assert row[2] is not None  # completed_at timestamp

        # Step 9: Verify task 1 marked complete in fix_plan.md
        is_marked = check_task_marked_complete(
            fix_plan_path=sample_fix_plan,
            task_id="task-1-first-task",
            task_number=1
        )
        assert is_marked is True

        content = sample_fix_plan.read_text()
        assert "### 1. First Task ✓" in content

        # Step 10: Verify task 2 claimed
        assert next_task is not None
        assert next_task['task_id'] == "task-2-second-task"

        # Step 11: Verify Ralph's current_task updated
        instance = await get_ralph_instance(ralph_id)
        assert instance.current_task_id == "task-2-second-task"

        print("✅ End-to-end workflow validated!")

    @pytest.mark.asyncio
    async def test_complete_workflow_all_three_locations(self, sample_fix_plan, mock_ralph_data_dir):
        """Test that completion updates all three locations: DB, fix_plan, and git commit SHA."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        ralph_id = "test-ralph-three-locations"
        await register_ralph_instance(ralph_id, project="test")

        # Claim task
        task = await claim_task(ralph_id, project="test")
        task_id = task['task_id']
        task_number = int(task_id.split('-')[1])

        # Simulate completion with commit
        commit_sha = "def4567890abcdef1234567890abcdef12345678"

        # Update all three locations
        # 1. Fix plan
        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id=task_id,
            task_number=task_number,
            mark_complete=True
        )

        # 2. Database (includes commit SHA)
        await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha=commit_sha,
            message="Task completed with commit"
        )

        # Verify all three locations
        # 1. Database
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT status, git_commit_sha FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == commit_sha

        # 2. Fix plan
        is_marked = check_task_marked_complete(
            fix_plan_path=sample_fix_plan,
            task_id=task_id,
            task_number=task_number
        )
        assert is_marked is True

        # 3. Git commit SHA is stored in database
        task_claim = await get_task_claim(task_id)
        assert task_claim.git_commit_sha == commit_sha

        print("✅ All three locations updated correctly!")

    @pytest.mark.asyncio
    async def test_workflow_handles_no_more_tasks(self, sample_fix_plan, mock_ralph_data_dir):
        """Test workflow when Ralph completes the last task."""
        # Create fix plan with only one task
        single_task_content = """# Fix Plan

## HIGH Priority

### 1. Only Task
This is the only task
"""
        sample_fix_plan.write_text(single_task_content)

        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        ralph_id = "test-ralph-last-task"
        await register_ralph_instance(ralph_id, project="test")

        # Claim the only task
        task = await claim_task(ralph_id, project="test")
        assert task is not None
        task_id = task['task_id']

        # Simulate completion
        log_path = get_ralph_log_path(ralph_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("""
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-1-only-task
COMMIT: final123abc456def789
---END_RALPH_STATUS---
""")

        # Check completion and mark complete
        detected_task_id, _, commit_sha = check_task_completion(ralph_id)
        assert detected_task_id == task_id

        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id=task_id,
            task_number=1,
            mark_complete=True
        )

        # Complete and try to claim next (should return None)
        next_task = await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha=commit_sha,
            message="Last task completed"
        )

        # Verify task is complete
        task_claim = await get_task_claim(task_id)
        assert task_claim.status.value == "completed"
        assert task_claim.git_commit_sha == commit_sha

        # Verify no next task
        assert next_task is None

        # Verify Ralph's current_task is None
        instance = await get_ralph_instance(ralph_id)
        assert instance.current_task_id is None

        print("✅ Last task completion handled correctly!")


class TestMultiRalphCoordination:
    """Test multi-Ralph coordination during task completion."""

    @pytest.mark.asyncio
    async def test_multiple_ralphs_complete_different_tasks(self, sample_fix_plan, mock_ralph_data_dir):
        """Test that multiple Ralphs can complete different tasks without conflicts."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")

        # Register two Ralphs
        ralph1_id = "test-ralph-multi-1"
        ralph2_id = "test-ralph-multi-2"
        await register_ralph_instance(ralph1_id, project="test")
        await register_ralph_instance(ralph2_id, project="test")

        # Each Ralph claims a different task
        task1 = await claim_task(ralph1_id, project="test")
        task2 = await claim_task(ralph2_id, project="test")

        assert task1['task_id'] != task2['task_id']

        # Both complete their tasks
        commit_sha1 = "ralph1commit1234567890abcdef12345678"
        commit_sha2 = "ralph2commit1234567890abcdef12345678"

        # Ralph 1 completes task 1
        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id=task1['task_id'],
            task_number=int(task1['task_id'].split('-')[1]),
            mark_complete=True
        )

        next1 = await complete_and_claim_next(
            ralph_id=ralph1_id,
            task_id=task1['task_id'],
            project="test",
            commit_sha=commit_sha1,
            message="Ralph 1 completed"
        )

        # Ralph 2 completes task 2
        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id=task2['task_id'],
            task_number=int(task2['task_id'].split('-')[1]),
            mark_complete=True
        )

        next2 = await complete_and_claim_next(
            ralph_id=ralph2_id,
            task_id=task2['task_id'],
            project="test",
            commit_sha=commit_sha2,
            message="Ralph 2 completed"
        )

        # Verify both tasks are complete
        claim1 = await get_task_claim(task1['task_id'])
        claim2 = await get_task_claim(task2['task_id'])

        assert claim1.status.value == "completed"
        assert claim2.status.value == "completed"
        assert claim1.git_commit_sha == commit_sha1
        assert claim2.git_commit_sha == commit_sha2

        # Verify both Ralphs claimed next tasks (task 3)
        # One should get it, the other should get None
        assert (next1 is not None) or (next2 is not None)

        # If both got a task, they should be different
        if next1 and next2:
            assert next1['task_id'] != next2['task_id']

        print("✅ Multi-Ralph coordination works correctly!")


class TestFailureScenarios:
    """Test failure handling in the completion workflow."""

    @pytest.mark.asyncio
    async def test_failed_task_handling(self, sample_fix_plan, mock_ralph_data_dir):
        """Test that FAILED status is properly handled."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        ralph_id = "test-ralph-failed"
        await register_ralph_instance(ralph_id, project="test")

        # Claim task
        task = await claim_task(ralph_id, project="test")
        task_id = task['task_id']

        # Simulate failure
        log_path = get_ralph_log_path(ralph_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("""
---RALPH_STATUS---
STATUS: FAILED
TASK_ID: task-1-first-task
REASON: Tests failed with 5 errors
---END_RALPH_STATUS---
""")

        # Check completion (should detect failure)
        detected_task_id, failure_reason, commit_sha = check_task_completion(ralph_id)

        assert detected_task_id == task_id
        assert failure_reason == "Tests failed with 5 errors"
        assert commit_sha is None

        print("✅ Failed task detection works correctly!")

    @pytest.mark.asyncio
    async def test_missing_commit_sha_handling(self, sample_fix_plan, mock_ralph_data_dir):
        """Test handling when commit SHA is missing from RALPH_STATUS."""
        await sync_tasks_from_fix_plan(sample_fix_plan, project="test")
        ralph_id = "test-ralph-no-sha"
        await register_ralph_instance(ralph_id, project="test")

        task = await claim_task(ralph_id, project="test")
        task_id = task['task_id']

        # Simulate completion without commit SHA
        log_path = get_ralph_log_path(ralph_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text("""
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-1-first-task
VERIFICATION: Task complete but forgot to commit
---END_RALPH_STATUS---
""")

        detected_task_id, failure, commit_sha = check_task_completion(ralph_id)

        assert detected_task_id == task_id
        assert failure is None
        assert commit_sha is None  # No commit SHA provided

        # Can still complete without commit SHA
        await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha=None,
            message="Completed without commit"
        )

        # Verify completion
        claim = await get_task_claim(task_id)
        assert claim.status.value == "completed"
        assert claim.git_commit_sha is None  # None is valid

        print("✅ Missing commit SHA handled gracefully!")
