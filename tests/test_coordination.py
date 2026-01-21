"""Tests for ChiefWiggum multi-instance task coordination.

Tests cover:
- Fix plan parsing (priorities, completion status, subtasks)
- Atomic claiming (no double claims, expired claim release)
- Instance heartbeat and crash detection
- Git commit guard verification
"""

import os
from datetime import datetime, timedelta
from unittest.mock import patch

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
    list_in_progress_tasks,
    list_pending_tasks,
    mark_stale_instances_crashed,
    register_ralph_instance,
    release_claim,
    reset_db,
    safe_git_commit,
    shutdown_instance,
    sync_tasks_from_fix_plan,
    verify_claim_before_commit,
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
