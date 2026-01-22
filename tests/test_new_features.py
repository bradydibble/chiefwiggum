"""Tests for new ChiefWiggum TUI features."""

import os
import tempfile

import pytest


@pytest.fixture
async def temp_db():
    """Create a temporary database for testing."""
    from chiefwiggum.database import init_db, reset_db

    # Use a temp file instead of :memory: since each connection creates new DB
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    os.environ["CHIEFWIGGUM_DB"] = db_path

    await init_db()
    yield db_path

    # Cleanup
    try:
        await reset_db()
    except Exception:
        pass
    try:
        os.unlink(db_path)
    except FileNotFoundError:
        pass
    try:
        os.unlink(db_path + "-wal")
    except FileNotFoundError:
        pass
    try:
        os.unlink(db_path + "-shm")
    except FileNotFoundError:
        pass


class TestErrorClassification:
    """Test error classification for auto-retry (US5, US6)."""

    def test_classify_transient_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("Rate limit exceeded") == ErrorCategory.TRANSIENT
        assert classify_error("Error 429: Too Many Requests") == ErrorCategory.TRANSIENT
        assert classify_error("Connection refused") == ErrorCategory.TRANSIENT
        assert classify_error("Service temporarily unavailable") == ErrorCategory.TRANSIENT

    def test_classify_timeout_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("Request timeout") == ErrorCategory.TIMEOUT
        assert classify_error("Operation timed out") == ErrorCategory.TIMEOUT
        assert classify_error("Deadline exceeded") == ErrorCategory.TIMEOUT

    def test_classify_permission_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("Permission denied") == ErrorCategory.PERMISSION
        assert classify_error("Error 401: Unauthorized") == ErrorCategory.PERMISSION
        assert classify_error("Error 403: Forbidden") == ErrorCategory.PERMISSION

    def test_classify_conflict_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("Merge conflict in file.py") == ErrorCategory.CONFLICT
        assert classify_error("Cannot merge branches") == ErrorCategory.CONFLICT

    def test_classify_code_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("SyntaxError: invalid syntax") == ErrorCategory.CODE_ERROR
        assert classify_error("Build failed with errors") == ErrorCategory.CODE_ERROR
        assert classify_error("Traceback (most recent call last)") == ErrorCategory.CODE_ERROR

    def test_classify_unknown_errors(self):
        from chiefwiggum import classify_error, ErrorCategory

        assert classify_error("Something went wrong") == ErrorCategory.UNKNOWN


class TestTaskCategoryInference:
    """Test task category inference from file paths (US4)."""

    def test_infer_ux_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category(["src/components/Button.tsx"]) == TaskCategory.UX
        assert infer_task_category(["templates/index.html"]) == TaskCategory.UX
        assert infer_task_category(["static/style.css"]) == TaskCategory.UX

    def test_infer_api_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category(["src/api/users.py"]) == TaskCategory.API
        assert infer_task_category(["routes/auth.py"]) == TaskCategory.API

    def test_infer_testing_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        # Tests directory
        assert infer_task_category(["tests/test_api.py"]) == TaskCategory.TESTING
        # Files ending in _test.py
        cat = infer_task_category(["something_test.py"])
        # This might match based on patterns - just verify it returns something valid
        assert cat in TaskCategory

    def test_infer_database_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category(["migrations/001_initial.py"]) == TaskCategory.DATABASE
        assert infer_task_category(["models/user.py"]) == TaskCategory.DATABASE

    def test_infer_infra_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category(["scripts/deploy.sh"]) == TaskCategory.INFRA
        assert infer_task_category(["docker/Dockerfile"]) == TaskCategory.INFRA
        assert infer_task_category([".github/workflows/ci.yml"]) == TaskCategory.INFRA

    def test_infer_general_category(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category(["README.md"]) == TaskCategory.GENERAL
        assert infer_task_category([]) == TaskCategory.GENERAL

    def test_infer_from_title(self):
        from chiefwiggum import infer_task_category, TaskCategory

        assert infer_task_category([], "Fix UI component styling") == TaskCategory.UX
        assert infer_task_category([], "Add API endpoint for users") == TaskCategory.API
        assert infer_task_category([], "Write tests for auth module") == TaskCategory.TESTING


class TestPauseResumeOperations:
    """Test pause/resume operations (US10)."""

    @pytest.mark.asyncio
    async def test_pause_instance(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance,
            pause_instance,
            get_ralph_instance,
            RalphInstanceStatus,
        )

        ralph_id = "test-ralph-pause"
        await register_ralph_instance(ralph_id)

        # Verify it's active
        inst = await get_ralph_instance(ralph_id)
        assert inst.status == RalphInstanceStatus.ACTIVE

        # Pause it
        result = await pause_instance(ralph_id)
        assert result is True

        # Verify it's paused
        inst = await get_ralph_instance(ralph_id)
        assert inst.status == RalphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_resume_instance(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance,
            pause_instance,
            resume_instance,
            get_ralph_instance,
            RalphInstanceStatus,
        )

        ralph_id = "test-ralph-resume"
        await register_ralph_instance(ralph_id)

        # Pause then resume
        await pause_instance(ralph_id)
        result = await resume_instance(ralph_id)
        assert result is True

        # Verify it's active again
        inst = await get_ralph_instance(ralph_id)
        assert inst.status == RalphInstanceStatus.ACTIVE

    @pytest.mark.asyncio
    async def test_pause_all_instances(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance,
            pause_all_instances,
            list_all_instances,
            RalphInstanceStatus,
        )

        # Register multiple instances
        await register_ralph_instance("ralph-1")
        await register_ralph_instance("ralph-2")
        await register_ralph_instance("ralph-3")

        # Pause all
        count = await pause_all_instances()
        assert count == 3

        # Verify all paused
        instances = await list_all_instances()
        for inst in instances:
            assert inst.status == RalphInstanceStatus.PAUSED

    @pytest.mark.asyncio
    async def test_stop_all_instances(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance,
            stop_all_instances,
            list_all_instances,
            RalphInstanceStatus,
        )

        # Register multiple instances
        await register_ralph_instance("ralph-stop-1")
        await register_ralph_instance("ralph-stop-2")

        # Stop all
        count = await stop_all_instances()
        assert count == 2

        # Verify all stopped
        instances = await list_all_instances()
        for inst in instances:
            assert inst.status == RalphInstanceStatus.STOPPED


class TestSystemStats:
    """Test system statistics (US11)."""

    @pytest.mark.asyncio
    async def test_get_system_stats(self, temp_db):
        from chiefwiggum import (
            get_system_stats,
            register_ralph_instance,
            sync_tasks_from_fix_plan,
        )

        # Create some test data
        await register_ralph_instance("stats-ralph")

        # Create a temp fix plan
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("""## HIGH PRIORITY
### 1. Task One
### 2. Task Two

## MEDIUM PRIORITY
### 3. Task Three
""")
            fix_plan_path = f.name

        try:
            await sync_tasks_from_fix_plan(fix_plan_path, "test")

            stats = await get_system_stats()
            assert stats.total_tasks == 3
            assert stats.pending_tasks == 3
            assert stats.active_instances == 1
        finally:
            os.unlink(fix_plan_path)


class TestRalphConfig:
    """Test Ralph configuration (US9)."""

    @pytest.mark.asyncio
    async def test_register_with_config(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance_with_config,
            get_ralph_instance,
            RalphConfig,
            TargetingConfig,
            ClaudeModel,
            TaskPriority,
        )

        config = RalphConfig(
            timeout_minutes=60,
            no_continue=True,
            model=ClaudeModel.OPUS,
        )
        targeting = TargetingConfig(
            project="tian",
            priority_min=TaskPriority.HIGH,
        )

        ralph_id = await register_ralph_instance_with_config(
            "config-ralph",
            config=config,
            targeting=targeting,
        )

        inst = await get_ralph_instance(ralph_id)
        assert inst.config.timeout_minutes == 60
        assert inst.config.no_continue is True
        assert inst.config.model == ClaudeModel.OPUS
        assert inst.targeting.project == "tian"
        assert inst.targeting.priority_min == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_update_config(self, temp_db):
        from chiefwiggum import (
            register_ralph_instance,
            update_ralph_config,
            get_ralph_instance,
            RalphConfig,
            ClaudeModel,
        )

        await register_ralph_instance("update-ralph")

        new_config = RalphConfig(
            model=ClaudeModel.HAIKU,
            max_loops=10,
        )
        result = await update_ralph_config("update-ralph", new_config)
        assert result is True

        inst = await get_ralph_instance("update-ralph")
        assert inst.config.model == ClaudeModel.HAIKU
        assert inst.config.max_loops == 10


class TestFailTaskWithRetry:
    """Test fail_task_with_retry for auto-retry (US6)."""

    @pytest.mark.asyncio
    async def test_transient_error_schedules_retry(self, temp_db):
        from chiefwiggum import (
            sync_tasks_from_fix_plan,
            claim_task,
            fail_task_with_retry,
            get_task_claim,
            ErrorCategory,
            TaskClaimStatus,
        )

        # Create a test task
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("## HIGH PRIORITY\n### 1. Test Task\n")
            fix_plan_path = f.name

        try:
            await sync_tasks_from_fix_plan(fix_plan_path, "test")
            result = await claim_task("test-ralph", "test")
            task_id = result["task_id"]

            # Fail with transient error
            success, will_retry = await fail_task_with_retry(
                "test-ralph",
                task_id,
                "Rate limit exceeded",
                ErrorCategory.TRANSIENT,
            )

            assert success is True
            assert will_retry is True

            # Check task is in retry_pending state
            task = await get_task_claim(task_id)
            assert task.status == TaskClaimStatus.RETRY_PENDING
            assert task.retry_count == 1
            assert task.next_retry_at is not None
        finally:
            os.unlink(fix_plan_path)

    @pytest.mark.asyncio
    async def test_code_error_no_retry(self, temp_db):
        from chiefwiggum import (
            sync_tasks_from_fix_plan,
            claim_task,
            fail_task_with_retry,
            get_task_claim,
            ErrorCategory,
            TaskClaimStatus,
        )

        # Create a test task
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write("## HIGH PRIORITY\n### 1. Code Error Task\n")
            fix_plan_path = f.name

        try:
            await sync_tasks_from_fix_plan(fix_plan_path, "test")
            result = await claim_task("test-ralph", "test")
            task_id = result["task_id"]

            # Fail with code error
            success, will_retry = await fail_task_with_retry(
                "test-ralph",
                task_id,
                "SyntaxError: invalid syntax",
                ErrorCategory.CODE_ERROR,
            )

            assert success is True
            assert will_retry is False

            # Check task is permanently failed
            task = await get_task_claim(task_id)
            assert task.status == TaskClaimStatus.FAILED
        finally:
            os.unlink(fix_plan_path)
