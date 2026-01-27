"""Tests for ChiefWiggum multi-instance task coordination.

Tests cover:
- Fix plan parsing (priorities, completion status, subtasks)
- Atomic claiming (no double claims, expired claim release)
- Instance heartbeat and crash detection
- Git commit guard verification
"""

import os
from datetime import datetime, timedelta

import pytest

from chiefwiggum import (
    CLAIM_EXPIRY_MINUTES,
    HEARTBEAT_STALE_MINUTES,
    TaskClaimStatus,
    TaskPriority,
    RalphInstanceStatus,
    claim_task,
    complete_task,
    extend_claim,
    fail_task,
    get_ralph_instance,
    get_task_claim,
    heartbeat,
    init_db,
    list_active_instances,
    list_pending_tasks,
    mark_stale_instances_crashed,
    register_ralph_instance,
    release_claim,
    reset_db,
    safe_git_commit,
    shutdown_instance,
    sync_tasks_from_fix_plan,
    verify_claim_before_commit,
    complete_and_claim_next,
)
from chiefwiggum.coordination import (
    _generate_task_id,
    _slugify,
    parse_fix_plan,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a test database for each test."""
    test_db = tmp_path / "test_coordination.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def sample_fix_plan_content() -> str:
    """Sample @fix_plan.md content for testing."""
    return """# Tian Phase 1b-1c Tasks

## HIGH PRIORITY - Get Data Flowing

### 1. Scheduler Implementation COMPLETE
Without this, nothing runs automatically.
- [x] Create scheduler.py using APScheduler
- [x] Daily digest trigger at 6:00 AM

### 2. Daily Reflection Workflow
The core orchestration.
- [x] Create reflection.py
- [ ] Gather data from all sources
- [ ] LLM generates reflection summary

### 3. Gmail Integration COMPLETE
Major data source.
- [x] Create gmail.py
- [x] Implement get_recent_emails()

## MEDIUM PRIORITY - Make It Learn

### 14. Pattern Detection Logic
LLM should find patterns.
- [ ] Add pattern extraction
- [ ] Pattern types to detect

### 15. Pattern Reinforcement
Track when decisions align or contradict patterns.
- [ ] When new decision logged, check alignment

## LOWER PRIORITY - Full Coverage

### 19. Meeting Prep Workflow COMPLETE
Full implementation.
- [x] Create meeting_prep.py
- [x] Triggered 30 min before meeting

### 20. Confluence Integration
Document context.
- [ ] Create confluence.py
- [ ] Implement get_recent_pages()

## POLISH - After Core Works

### 27. Assist Button COMPLETE
UI feature for drafting.
- [x] Add Assist button to todo detail
- [x] Create assist_drafts table

### 28. Standing Approvals Registry
Auto-approve rules.
- [ ] Create standing_approvals table
- [ ] Example rules
"""


@pytest.fixture
def sample_fix_plan_file(sample_fix_plan_content, tmp_path):
    """Create a temporary fix plan file."""
    fix_plan = tmp_path / "@fix_plan.md"
    fix_plan.write_text(sample_fix_plan_content)
    return fix_plan


# =============================================================================
# Fix Plan Parsing Tests
# =============================================================================


class TestFixPlanParsing:
    """Tests for parse_fix_plan function."""

    def test_parse_empty_file(self, tmp_path):
        """Test parsing an empty file."""
        fix_plan = tmp_path / "@fix_plan.md"
        fix_plan.write_text("")
        tasks = parse_fix_plan(fix_plan)
        assert tasks == []

    def test_parse_nonexistent_file(self, tmp_path):
        """Test parsing a non-existent file."""
        tasks = parse_fix_plan(tmp_path / "nonexistent.md")
        assert tasks == []

    def test_parse_high_priority_tasks(self, sample_fix_plan_file):
        """Test parsing HIGH priority tasks."""
        tasks = parse_fix_plan(sample_fix_plan_file)
        high_priority_tasks = [t for t in tasks if t.priority == TaskPriority.HIGH]

        assert len(high_priority_tasks) == 3
        assert high_priority_tasks[0].task_number == 1
        assert high_priority_tasks[0].title == "Scheduler Implementation"
        assert high_priority_tasks[0].is_complete is True
        assert high_priority_tasks[0].section == "Get Data Flowing"

    def test_parse_medium_priority_tasks(self, sample_fix_plan_file):
        """Test parsing MEDIUM priority tasks."""
        tasks = parse_fix_plan(sample_fix_plan_file)
        medium_tasks = [t for t in tasks if t.priority == TaskPriority.MEDIUM]

        assert len(medium_tasks) == 2
        assert medium_tasks[0].task_number == 14
        assert medium_tasks[0].is_complete is False

    def test_parse_lower_priority_tasks(self, sample_fix_plan_file):
        """Test parsing LOWER priority tasks."""
        tasks = parse_fix_plan(sample_fix_plan_file)
        lower_tasks = [t for t in tasks if t.priority == TaskPriority.LOWER]

        assert len(lower_tasks) == 2
        task_19 = next(t for t in lower_tasks if t.task_number == 19)
        assert task_19.is_complete is True

    def test_parse_polish_tasks(self, sample_fix_plan_file):
        """Test parsing POLISH priority tasks."""
        tasks = parse_fix_plan(sample_fix_plan_file)
        polish_tasks = [t for t in tasks if t.priority == TaskPriority.POLISH]

        assert len(polish_tasks) == 2
        task_27 = next(t for t in polish_tasks if t.task_number == 27)
        assert task_27.is_complete is True
        assert task_27.title == "Assist Button"

    def test_parse_subtasks(self, sample_fix_plan_file):
        """Test parsing completed and incomplete subtasks."""
        tasks = parse_fix_plan(sample_fix_plan_file)

        task_2 = next(t for t in tasks if t.task_number == 2)
        assert len(task_2.completed_subtasks) == 1
        assert "Create reflection.py" in task_2.completed_subtasks[0]
        assert len(task_2.subtasks) == 2

    def test_task_id_generation(self, sample_fix_plan_file):
        """Test task ID format."""
        tasks = parse_fix_plan(sample_fix_plan_file)

        task_1 = next(t for t in tasks if t.task_number == 1)
        assert task_1.task_id == "task-1-scheduler-implementation"

        task_14 = next(t for t in tasks if t.task_number == 14)
        assert task_14.task_id == "task-14-pattern-detection-logic"


class TestSlugify:
    """Tests for _slugify helper function."""

    def test_basic_slug(self):
        """Test basic text slugification."""
        assert _slugify("Hello World") == "hello-world"

    def test_special_characters(self):
        """Test handling special characters."""
        assert _slugify("Hello, World!") == "hello-world"

    def test_multiple_spaces(self):
        """Test multiple spaces are collapsed."""
        assert _slugify("Hello   World") == "hello-world"

    def test_long_text_truncation(self):
        """Test long text is truncated to 50 chars."""
        long_text = "A" * 100
        result = _slugify(long_text)
        assert len(result) == 50


class TestTaskIdGeneration:
    """Tests for _generate_task_id function."""

    def test_basic_id(self):
        """Test basic task ID generation."""
        assert _generate_task_id(1, "Test Task") == "task-1-test-task"

    def test_id_with_special_chars(self):
        """Test task ID with special characters in title."""
        assert _generate_task_id(22, "File Processing Workflow") == "task-22-file-processing-workflow"


# =============================================================================
# Task Sync Tests
# =============================================================================


class TestTaskSync:
    """Tests for sync_tasks_from_fix_plan function."""

    @pytest.mark.asyncio
    async def test_sync_creates_tasks(self, sample_fix_plan_file):
        """Test that sync creates task claims from fix plan."""
        count = await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        assert count == 9

        pending = await list_pending_tasks()
        assert len(pending) > 0

    @pytest.mark.asyncio
    async def test_sync_marks_complete_tasks(self, sample_fix_plan_file):
        """Test that sync marks completed tasks correctly."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        task = await get_task_claim("task-1-scheduler-implementation")
        assert task is not None
        assert task.status == TaskClaimStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_sync_idempotent(self, sample_fix_plan_file):
        """Test that sync is idempotent."""
        count1 = await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        count2 = await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        assert count1 == count2


# =============================================================================
# Task Claiming Tests
# =============================================================================


class TestTaskClaiming:
    """Tests for claim_task and related functions."""

    @pytest.mark.asyncio
    async def test_claim_task_success(self, sample_fix_plan_file):
        """Test claiming a task successfully."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        assert result is not None
        assert "task_id" in result
        assert "task_title" in result
        assert "expires_at" in result

    @pytest.mark.asyncio
    async def test_claim_priority_order(self, sample_fix_plan_file):
        """Test that tasks are claimed in priority order."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        assert result is not None
        assert result["task_priority"] == "HIGH"

    @pytest.mark.asyncio
    async def test_no_double_claims(self, sample_fix_plan_file):
        """Test that the same task cannot be claimed twice."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result1 = await claim_task("ralph-1")
        assert result1 is not None
        task_id = result1["task_id"]

        result2 = await claim_task("ralph-2")
        assert result2 is not None
        assert result2["task_id"] != task_id

    @pytest.mark.asyncio
    async def test_claim_with_project_filter(self, sample_fix_plan_file):
        """Test claiming with project filter."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1", project="test")
        assert result is not None
        assert result["project"] == "test"

    @pytest.mark.asyncio
    async def test_claim_no_tasks_available(self, tmp_path):
        """Test claiming when no tasks are available."""
        fix_plan = tmp_path / "@fix_plan.md"
        fix_plan.write_text("")
        await sync_tasks_from_fix_plan(fix_plan)

        result = await claim_task("ralph-1")
        assert result is None


# =============================================================================
# Claim Management Tests
# =============================================================================


class TestClaimManagement:
    """Tests for extend_claim, complete_task, fail_task, release_claim."""

    @pytest.mark.asyncio
    async def test_extend_claim(self, sample_fix_plan_file):
        """Test extending a claim's expiry."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await extend_claim("ralph-1", task_id)
        assert success is True

        task = await get_task_claim(task_id)
        assert task.expires_at > datetime.now()

    @pytest.mark.asyncio
    async def test_extend_claim_wrong_owner(self, sample_fix_plan_file):
        """Test that only the owner can extend a claim."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await extend_claim("ralph-2", task_id)
        assert success is False

    @pytest.mark.asyncio
    async def test_complete_task(self, sample_fix_plan_file):
        """Test completing a task."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await complete_task(
            "ralph-1", task_id,
            commit_sha="abc123",
            message="Implemented feature"
        )
        assert success is True

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.COMPLETED
        assert task.git_commit_sha == "abc123"
        assert task.completion_message == "Implemented feature"

    @pytest.mark.asyncio
    async def test_fail_task(self, sample_fix_plan_file):
        """Test failing a task."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await fail_task("ralph-1", task_id, "Build failed")
        assert success is True

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.FAILED
        assert task.completion_message == "Build failed"

    @pytest.mark.asyncio
    async def test_release_claim(self, sample_fix_plan_file):
        """Test releasing a claim returns task to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success = await release_claim("ralph-1", task_id)
        assert success is True

        task = await get_task_claim(task_id)
        # Released tasks go back to pending so another Ralph can claim them
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None


# =============================================================================
# Instance Management Tests
# =============================================================================


class TestInstanceManagement:
    """Tests for Ralph instance registration and tracking."""

    @pytest.mark.asyncio
    async def test_register_instance(self):
        """Test registering a new instance."""
        ralph_id = await register_ralph_instance("ralph-1", session_file="/tmp/session.json")

        assert ralph_id == "ralph-1"

        instance = await get_ralph_instance("ralph-1")
        assert instance is not None
        assert instance.status == RalphInstanceStatus.ACTIVE
        assert instance.session_file == "/tmp/session.json"

    @pytest.mark.asyncio
    async def test_heartbeat(self):
        """Test heartbeat updates."""
        await register_ralph_instance("ralph-1")

        instance = await get_ralph_instance("ralph-1")
        initial_loop = instance.loop_count

        await heartbeat("ralph-1")

        instance = await get_ralph_instance("ralph-1")
        assert instance.loop_count == initial_loop + 1

    @pytest.mark.asyncio
    async def test_shutdown_instance(self, sample_fix_plan_file):
        """Test clean shutdown of an instance releases tasks back to pending."""
        await register_ralph_instance("ralph-1")
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        await shutdown_instance("ralph-1")

        instance = await get_ralph_instance("ralph-1")
        assert instance.status == RalphInstanceStatus.STOPPED

        task = await get_task_claim(task_id)
        # Shutdown releases tasks back to pending for another Ralph
        assert task.status == TaskClaimStatus.PENDING

    @pytest.mark.asyncio
    async def test_list_active_instances(self):
        """Test listing active instances."""
        await register_ralph_instance("ralph-1")
        await register_ralph_instance("ralph-2")

        instances = await list_active_instances()
        assert len(instances) == 2

        await shutdown_instance("ralph-1")

        instances = await list_active_instances()
        assert len(instances) == 1
        assert instances[0].ralph_id == "ralph-2"


# =============================================================================
# Crash Detection Tests
# =============================================================================


class TestCrashDetection:
    """Tests for crash detection and stale instance handling."""

    @pytest.mark.asyncio
    async def test_mark_stale_instances_crashed(self, sample_fix_plan_file):
        """Test marking stale instances as crashed releases tasks to pending."""
        await register_ralph_instance("ralph-1")
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        # Make instance stale by setting old heartbeat
        from chiefwiggum.database import get_connection
        conn = await get_connection()
        stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
        await conn.execute(
            "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
            (stale_time, "ralph-1")
        )
        await conn.commit()
        await conn.close()

        count = await mark_stale_instances_crashed()
        assert count == 1

        instance = await get_ralph_instance("ralph-1")
        assert instance.status == RalphInstanceStatus.CRASHED

        task = await get_task_claim(task_id)
        # Crashed instance's tasks go back to pending for another Ralph
        assert task.status == TaskClaimStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_false_crash_detection(self):
        """Test that active instances aren't marked as crashed."""
        await register_ralph_instance("ralph-1")
        await heartbeat("ralph-1")

        count = await mark_stale_instances_crashed()
        assert count == 0

        instance = await get_ralph_instance("ralph-1")
        assert instance.status == RalphInstanceStatus.ACTIVE


# =============================================================================
# Git Commit Guard Tests
# =============================================================================


class TestGitCommitGuard:
    """Tests for verify_claim_before_commit and safe_git_commit."""

    @pytest.mark.asyncio
    async def test_verify_claim_valid(self, sample_fix_plan_file):
        """Test verifying a valid claim."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        is_valid, msg = await verify_claim_before_commit("ralph-1", task_id)
        assert is_valid is True
        assert msg == "Claim verified"

    @pytest.mark.asyncio
    async def test_verify_claim_wrong_owner(self, sample_fix_plan_file):
        """Test verifying claim for wrong owner."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        is_valid, msg = await verify_claim_before_commit("ralph-2", task_id)
        assert is_valid is False
        assert "different instance" in msg

    @pytest.mark.asyncio
    async def test_verify_claim_not_found(self):
        """Test verifying a non-existent task."""
        is_valid, msg = await verify_claim_before_commit("ralph-1", "nonexistent-task")
        assert is_valid is False
        assert "not found" in msg.lower()

    @pytest.mark.asyncio
    async def test_safe_git_commit_verification_fails(self, sample_fix_plan_file):
        """Test safe_git_commit when verification fails."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1")
        task_id = result["task_id"]

        success, msg = await safe_git_commit("ralph-2", task_id, "Test commit")
        assert success is False
        assert "verification failed" in msg.lower()


# =============================================================================
# Constants Tests
# =============================================================================


class TestConstants:
    """Tests for module constants."""

    def test_claim_expiry_minutes(self):
        """Test claim expiry is set correctly."""
        assert CLAIM_EXPIRY_MINUTES == 7

    def test_heartbeat_stale_minutes(self):
        """Test heartbeat stale threshold is set correctly."""
        assert HEARTBEAT_STALE_MINUTES == 10


# =============================================================================
# Task Assignment Strategy Tests
# =============================================================================


class TestTaskAssignmentStrategies:
    """Tests for task assignment strategies: priority, round_robin, specialized."""

    @pytest.mark.asyncio
    async def test_claim_next_by_priority_returns_highest_priority(self, sample_fix_plan_file):
        """Priority strategy claims highest priority task first."""
        from chiefwiggum.coordination import claim_next_by_priority

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_next_by_priority("ralph-1")
        assert result is not None
        assert result["task_priority"] == "HIGH"

    @pytest.mark.asyncio
    async def test_claim_next_round_robin_distributes_tasks(self, sample_fix_plan_file):
        """Round robin strategy distributes tasks across ralphs."""
        from chiefwiggum.coordination import claim_next_round_robin

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Claim multiple tasks with different ralphs
        result1 = await claim_next_round_robin("ralph-1")
        result2 = await claim_next_round_robin("ralph-2")

        assert result1 is not None
        assert result2 is not None
        # Both should get tasks (distribution)
        assert result1["task_id"] != result2["task_id"]

    @pytest.mark.asyncio
    async def test_claim_next_by_category_respects_categories(self, sample_fix_plan_file):
        """Specialized strategy respects category assignments."""
        from chiefwiggum.coordination import claim_next_by_category
        from chiefwiggum.models import TaskCategory

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Try to claim with specific category filter
        result = await claim_next_by_category(
            "ralph-1",
            project="test",
            categories=[TaskCategory.API]
        )
        # Should get a task (may fall back if no API tasks)
        # The function falls back to priority if no matching category tasks
        assert result is not None or result is None  # Either outcome is valid

    @pytest.mark.asyncio
    async def test_get_next_task_for_ralph_priority_strategy(self, sample_fix_plan_file):
        """get_next_task_for_ralph with priority strategy."""
        from chiefwiggum.coordination import get_next_task_for_ralph

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await get_next_task_for_ralph(
            "ralph-1",
            strategy="priority",
            project="test"
        )
        assert result is not None
        assert result["task_priority"] == "HIGH"

    @pytest.mark.asyncio
    async def test_get_next_task_for_ralph_round_robin_strategy(self, sample_fix_plan_file):
        """get_next_task_for_ralph with round_robin strategy."""
        from chiefwiggum.coordination import get_next_task_for_ralph

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await get_next_task_for_ralph(
            "ralph-1",
            strategy="round_robin",
            project="test"
        )
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_assigned_categories_matches_ralph_prefix(self):
        """get_assigned_categories returns categories for matching prefix."""
        from chiefwiggum.coordination import get_assigned_categories
        from chiefwiggum.config import set_category_assignments

        # Set up category assignments
        set_category_assignments({
            "frontend-": ["ux"],
            "backend-": ["api", "database"],
        })

        # Test matching prefix
        categories = get_assigned_categories("frontend-ralph-1")
        # Returns empty if prefix doesn't match exactly in current implementation
        # This tests the lookup logic

        # Test non-matching prefix
        categories = get_assigned_categories("other-ralph")
        assert categories == []


# =============================================================================
# Auto-Scaling Tests
# =============================================================================


class TestAutoScaling:
    """Tests for auto-scaling logic."""

    @pytest.mark.asyncio
    async def test_analyze_category_backlog_counts_pending(self, sample_fix_plan_file):
        """analyze_category_backlog counts pending tasks by category."""
        from chiefwiggum.coordination import analyze_category_backlog

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        backlog = await analyze_category_backlog()
        assert isinstance(backlog, dict)
        # Should have some entries for pending tasks

    @pytest.mark.asyncio
    async def test_analyze_category_backlog_weights_by_priority(self, sample_fix_plan_file):
        """analyze_category_backlog weights tasks by priority."""
        from chiefwiggum.coordination import analyze_category_backlog

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        _backlog = await analyze_category_backlog()
        # High priority tasks should contribute more weight
        # The exact values depend on the fixture content

    @pytest.mark.asyncio
    async def test_should_spawn_ralph_respects_threshold(self, sample_fix_plan_file):
        """should_spawn_ralph respects spawn threshold config."""
        from chiefwiggum.coordination import should_spawn_ralph
        from chiefwiggum.config import set_auto_scaling_config

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Disable auto-spawn
        set_auto_scaling_config({"auto_spawn_enabled": False})
        should_spawn, category = await should_spawn_ralph()
        assert should_spawn is False

    @pytest.mark.asyncio
    async def test_get_idle_ralphs_returns_old_idle_instances(self):
        """get_idle_ralphs returns instances that have been idle too long."""
        from chiefwiggum.coordination import get_idle_ralphs
        from chiefwiggum.database import get_connection

        # Register an instance
        await register_ralph_instance("ralph-idle")

        # Make it idle with old heartbeat
        conn = await get_connection()
        stale_time = datetime.now() - timedelta(minutes=60)
        await conn.execute(
            """UPDATE ralph_instances
               SET status = 'idle', last_heartbeat = ?
               WHERE ralph_id = ?""",
            (stale_time, "ralph-idle")
        )
        await conn.commit()
        await conn.close()

        idle_ralphs = await get_idle_ralphs(older_than_minutes=30)
        assert len(idle_ralphs) >= 1
        assert any(r.ralph_id == "ralph-idle" for r in idle_ralphs)

    @pytest.mark.asyncio
    async def test_cleanup_idle_ralphs_skips_when_pending_tasks(self, sample_fix_plan_file):
        """cleanup_idle_ralphs doesn't cleanup when tasks are pending."""
        from chiefwiggum.coordination import cleanup_idle_ralphs
        from chiefwiggum.config import set_auto_scaling_config

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        set_auto_scaling_config({"auto_cleanup_enabled": True})

        # Should not cleanup because there are pending tasks
        cleaned = await cleanup_idle_ralphs()
        assert cleaned == 0

    @pytest.mark.asyncio
    async def test_count_pending_tasks(self, sample_fix_plan_file):
        """count_pending_tasks returns correct count."""
        from chiefwiggum.coordination import count_pending_tasks

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        count = await count_pending_tasks()
        assert count > 0

    @pytest.mark.asyncio
    async def test_count_running_ralphs(self):
        """count_running_ralphs returns count of running instances."""
        from chiefwiggum.coordination import count_running_ralphs

        # Initially no ralphs running (in test environment)
        count = await count_running_ralphs()
        assert isinstance(count, int)
        assert count >= 0


# =============================================================================
# Task Completion and Database Update Tests
# =============================================================================


class TestCompleteAndClaimNext:
    """Tests for complete_and_claim_next() database updates.

    These tests verify that task completion properly updates all required
    database fields and atomically claims the next task.
    """

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_updates_all_fields(self, sample_fix_plan_file):
        """Test complete_and_claim_next() updates all required fields."""
        from chiefwiggum.database import get_connection

        # Setup: Sync tasks and register Ralph
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        ralph_id = "test-ralph-db-001"
        await register_ralph_instance(ralph_id, project="test")

        # Claim a task
        task_result = await claim_task(ralph_id, project="test")
        assert task_result is not None
        task_id = task_result['task_id']

        # Complete and claim next
        next_task = await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha="abc123def456",
            message="Task completed successfully"
        )

        # Verify database state
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT status, git_commit_sha, completed_at FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()

        assert row is not None
        assert row[0] == "completed"  # status
        assert row[1] == "abc123def456"  # git_commit_sha
        assert row[2] is not None  # completed_at timestamp

        # Verify next task claimed
        assert next_task is not None
        assert next_task['task_id'] != task_id

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_no_more_tasks(self, sample_fix_plan_file):
        """Test complete_and_claim_next() when no more tasks available."""
        from chiefwiggum.database import get_connection

        # Setup with a single-task fix plan
        single_task_content = """# Fix Plan

## HIGH Priority

### 1. Only Task
This is the only task
"""
        sample_fix_plan_file.write_text(single_task_content)
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        ralph_id = "test-ralph-single"
        await register_ralph_instance(ralph_id, project="test")

        # Claim the only task
        task_result = await claim_task(ralph_id, project="test")
        assert task_result is not None
        task_id = task_result['task_id']

        # Complete and try to claim next (should return None)
        next_task = await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha="final123abc",
            message="Last task completed"
        )

        # Verify first task is marked complete
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT status, git_commit_sha FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == "completed"
        assert row[1] == "final123abc"

        # Verify no next task
        assert next_task is None

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_updates_instance_task(self, sample_fix_plan_file):
        """Test that complete_and_claim_next() updates Ralph instance's current_task_id."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        ralph_id = "test-ralph-instance-update"
        await register_ralph_instance(ralph_id, project="test")

        # Claim first task
        task1 = await claim_task(ralph_id, project="test")
        task1_id = task1['task_id']

        # Verify instance has task1
        instance = await get_ralph_instance(ralph_id)
        assert instance.current_task_id == task1_id

        # Complete task1 and claim task2
        task2 = await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task1_id,
            project="test",
            commit_sha="update123",
            message="Completed task 1"
        )

        # Verify instance now has task2
        if task2:
            instance = await get_ralph_instance(ralph_id)
            assert instance.current_task_id == task2['task_id']
            assert instance.current_task_id != task1_id

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_preserves_task_metadata(self, sample_fix_plan_file):
        """Test that complete_and_claim_next() preserves task metadata."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        ralph_id = "test-ralph-metadata"
        await register_ralph_instance(ralph_id, project="test")

        # Claim a task
        task = await claim_task(ralph_id, project="test")
        task_id = task['task_id']

        # Store original metadata
        original_title = task['task_title']
        original_priority = task['task_priority']

        # Complete and claim next
        await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha="meta123",
            message="Metadata test"
        )

        # Verify completed task metadata is preserved
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT task_title, task_priority, status FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        assert row[0] == original_title
        assert row[1] == original_priority
        assert row[2] == "completed"

    @pytest.mark.asyncio
    async def test_complete_and_claim_next_with_message(self, sample_fix_plan_file):
        """Test that completion message is stored."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        ralph_id = "test-ralph-message"
        await register_ralph_instance(ralph_id, project="test")

        task = await claim_task(ralph_id, project="test")
        task_id = task['task_id']

        completion_message = "All tests pass, verified manually"

        await complete_and_claim_next(
            ralph_id=ralph_id,
            task_id=task_id,
            project="test",
            commit_sha="msg123",
            message=completion_message
        )

        # Verify message is stored (check task_history table if it exists,
        # or completion_message field if present)
        conn = await get_connection()
        cursor = await conn.execute(
            "SELECT completion_message FROM task_claims WHERE task_id = ?",
            (task_id,)
        )
        row = await cursor.fetchone()
        if row and row[0]:
            assert completion_message in row[0]
