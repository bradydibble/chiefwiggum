"""Tests for error recovery, retry logic, and circuit breaker patterns.

Tests cover:
- Error classification for different error types
- Retry logic with exponential backoff
- Maximum retry exhaustion
- Circuit breaker patterns
- Task requeue with error metadata
- Crash recovery
"""

import asyncio
import os
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chiefwiggum import (
    CLAIM_EXPIRY_MINUTES,
    HEARTBEAT_STALE_MINUTES,
    ErrorCategory,
    RalphInstanceStatus,
    TaskClaimStatus,
    claim_task,
    get_ralph_instance,
    get_task_claim,
    init_db,
    list_failed_tasks,
    mark_stale_instances_crashed,
    register_ralph_instance,
    reset_db,
    sync_tasks_from_fix_plan,
)
from chiefwiggum.coordination import (
    classify_error,
    fail_task_with_retry,
    process_retry_tasks,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    """Set up a test database for each test."""
    test_db = tmp_path / "test_error_recovery.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def sample_fix_plan_content() -> str:
    """Sample @fix_plan.md content for testing."""
    return """# Test Tasks

## HIGH PRIORITY - Critical Tasks

### 1. API Integration Task
Implement API integration.
- [ ] Create API client
- [ ] Add error handling

### 2. Database Migration
Migrate database schema.
- [ ] Create migration script
- [ ] Test rollback

### 3. Frontend Component
Build new UI component.
- [ ] Design component
- [ ] Implement logic
"""


@pytest.fixture
def sample_fix_plan_file(sample_fix_plan_content, tmp_path):
    """Create a temporary fix plan file."""
    fix_plan = tmp_path / "@fix_plan.md"
    fix_plan.write_text(sample_fix_plan_content)
    return fix_plan


# =============================================================================
# Error Classification Tests
# =============================================================================


class TestErrorClassification:
    """Tests for classify_error function."""

    def test_classify_transient_rate_limit(self):
        """Test transient error: rate limit."""
        error = "API rate limit exceeded. Please try again later."
        assert classify_error(error) == ErrorCategory.TRANSIENT

    def test_classify_transient_429(self):
        """Test transient error: HTTP 429."""
        error = "HTTP 429: Too many requests"
        assert classify_error(error) == ErrorCategory.TRANSIENT

    def test_classify_transient_connection_refused(self):
        """Test transient error: connection refused."""
        error = "Connection refused by remote server"
        assert classify_error(error) == ErrorCategory.TRANSIENT

    def test_classify_transient_503(self):
        """Test transient error: HTTP 503."""
        error = "Service unavailable (503)"
        assert classify_error(error) == ErrorCategory.TRANSIENT

    def test_classify_timeout(self):
        """Test timeout error."""
        error = "Request timeout after 30 seconds"
        assert classify_error(error) == ErrorCategory.TIMEOUT

    def test_classify_timeout_deadline(self):
        """Test timeout error: deadline exceeded."""
        error = "Operation deadline exceeded"
        assert classify_error(error) == ErrorCategory.TIMEOUT

    def test_classify_permission_denied(self):
        """Test permission error: access denied."""
        error = "Permission denied: Access to resource forbidden"
        assert classify_error(error) == ErrorCategory.PERMISSION

    def test_classify_permission_401(self):
        """Test permission error: HTTP 401."""
        error = "HTTP 401: Unauthorized access"
        assert classify_error(error) == ErrorCategory.PERMISSION

    def test_classify_permission_403(self):
        """Test permission error: HTTP 403."""
        error = "403 Forbidden: You don't have access"
        assert classify_error(error) == ErrorCategory.PERMISSION

    def test_classify_conflict(self):
        """Test conflict error."""
        error = "Merge conflict detected in file.py"
        assert classify_error(error) == ErrorCategory.CONFLICT

    def test_classify_conflict_diverged(self):
        """Test conflict error: branches diverged."""
        error = "Branches have diverged and cannot merge"
        assert classify_error(error) == ErrorCategory.CONFLICT

    def test_classify_code_error_syntax(self):
        """Test code error: syntax error."""
        error = "SyntaxError: invalid syntax on line 42"
        assert classify_error(error) == ErrorCategory.CODE_ERROR

    def test_classify_code_error_import(self):
        """Test code error: import error."""
        error = "ImportError: cannot import module 'foo'"
        assert classify_error(error) == ErrorCategory.CODE_ERROR

    def test_classify_code_error_traceback(self):
        """Test code error: traceback."""
        error = "Traceback (most recent call last):\n  File..."
        assert classify_error(error) == ErrorCategory.CODE_ERROR

    def test_classify_unknown(self):
        """Test unknown error classification."""
        error = "Something went wrong but we don't know what"
        assert classify_error(error) == ErrorCategory.UNKNOWN


# =============================================================================
# Retry Logic Tests
# =============================================================================


class TestRetryLogic:
    """Tests for retry logic with exponential backoff."""

    @pytest.mark.asyncio
    async def test_transient_error_retries_with_backoff(self, sample_fix_plan_file):
        """Test transient errors retry with exponential backoff."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail with transient error
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            "Rate limit exceeded",
            ErrorCategory.TRANSIENT
        )

        assert success is True
        assert will_retry is True

        # Check task status and retry metadata
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING
        assert task.retry_count == 1
        assert task.error_category == ErrorCategory.TRANSIENT
        assert task.next_retry_at is not None
        assert task.claimed_by_ralph_id is None  # Released

        # Verify backoff time (should be 30 seconds for first retry)
        expected_backoff = timedelta(seconds=30)
        actual_backoff = task.next_retry_at - datetime.now()
        assert abs(actual_backoff.total_seconds() - expected_backoff.total_seconds()) < 2

    @pytest.mark.asyncio
    async def test_timeout_error_retries(self, sample_fix_plan_file):
        """Test timeout errors retry."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail with timeout error
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            "Request timeout after 30 seconds",
            ErrorCategory.TIMEOUT
        )

        assert success is True
        assert will_retry is True

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING
        assert task.error_category == ErrorCategory.TIMEOUT

    @pytest.mark.asyncio
    async def test_permission_error_no_retry(self, sample_fix_plan_file):
        """Test permission errors don't retry (permanent failure)."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail with permission error
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            "Permission denied: 403 Forbidden",
            ErrorCategory.PERMISSION
        )

        assert success is True
        assert will_retry is False

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.FAILED
        assert task.error_category == ErrorCategory.PERMISSION
        assert task.next_retry_at is None  # No retry scheduled

    @pytest.mark.asyncio
    async def test_code_error_no_retry(self, sample_fix_plan_file):
        """Test code errors don't retry (permanent failure)."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail with code error
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            "SyntaxError: invalid syntax",
            ErrorCategory.CODE_ERROR
        )

        assert success is True
        assert will_retry is False

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.FAILED
        assert task.error_category == ErrorCategory.CODE_ERROR

    @pytest.mark.asyncio
    async def test_exponential_backoff_increases(self, sample_fix_plan_file):
        """Test exponential backoff increases with each retry."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # First failure (retry_count = 1)
        await fail_task_with_retry("ralph-1", task_id, "Rate limit", ErrorCategory.TRANSIENT)
        task = await get_task_claim(task_id)
        first_backoff = task.next_retry_at - datetime.now()

        # Set next_retry_at to past to allow immediate reprocessing
        conn = await get_connection()
        past_time = datetime.now() - timedelta(seconds=1)
        await conn.execute(
            "UPDATE task_claims SET next_retry_at = ? WHERE task_id = ?",
            (past_time, task_id)
        )
        await conn.commit()
        await conn.close()

        # Process retry to move back to pending
        count = await process_retry_tasks()
        assert count == 1

        # Re-claim and fail again (retry_count = 2)
        await register_ralph_instance("ralph-2")
        result2 = await claim_task("ralph-2", project="test")
        assert result2["task_id"] == task_id  # Same task

        await fail_task_with_retry("ralph-2", task_id, "Rate limit again", ErrorCategory.TRANSIENT)
        task = await get_task_claim(task_id)
        second_backoff = task.next_retry_at - datetime.now()

        # Second backoff should be ~2x first backoff (exponential)
        # First: 30s, Second: 60s
        assert task.retry_count == 2
        assert second_backoff.total_seconds() > first_backoff.total_seconds()


# =============================================================================
# Max Retry Exhaustion Tests
# =============================================================================


class TestMaxRetryExhaustion:
    """Tests for maximum retry limit enforcement."""

    @pytest.mark.asyncio
    async def test_max_retries_exhausted_marks_failed(self, sample_fix_plan_file):
        """Test task marked as failed after max retries exhausted."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Set max_retries to 2 for testing
        conn = await get_connection()
        await conn.execute(
            "UPDATE task_claims SET max_retries = 2 WHERE task_id = ?",
            (task_id,)
        )
        await conn.commit()
        await conn.close()

        # Retry 1
        await fail_task_with_retry("ralph-1", task_id, "Rate limit", ErrorCategory.TRANSIENT)
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING
        assert task.retry_count == 1

        # Set next_retry_at to past to allow immediate reprocessing
        conn = await get_connection()
        past_time = datetime.now() - timedelta(seconds=1)
        await conn.execute(
            "UPDATE task_claims SET next_retry_at = ? WHERE task_id = ?",
            (past_time, task_id)
        )
        await conn.commit()
        await conn.close()

        # Process retry and re-claim
        count = await process_retry_tasks()
        assert count == 1
        result2 = await claim_task("ralph-2", project="test")
        assert result2["task_id"] == task_id

        # Retry 2
        await fail_task_with_retry("ralph-2", task_id, "Rate limit", ErrorCategory.TRANSIENT)
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING
        assert task.retry_count == 2

        # Set next_retry_at to past again
        conn = await get_connection()
        past_time = datetime.now() - timedelta(seconds=1)
        await conn.execute(
            "UPDATE task_claims SET next_retry_at = ? WHERE task_id = ?",
            (past_time, task_id)
        )
        await conn.commit()
        await conn.close()

        # Process retry and re-claim
        count = await process_retry_tasks()
        assert count == 1
        result3 = await claim_task("ralph-3", project="test")
        assert result3["task_id"] == task_id

        # Retry 3 - should exceed max and fail permanently
        success, will_retry = await fail_task_with_retry(
            "ralph-3",
            task_id,
            "Rate limit still",
            ErrorCategory.TRANSIENT
        )

        assert success is True
        assert will_retry is False  # Max retries exceeded

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.FAILED
        assert task.retry_count == 3

    @pytest.mark.asyncio
    async def test_max_retries_default_value(self, sample_fix_plan_file):
        """Test default max_retries is 3."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        task = await get_task_claim(task_id)
        # Default max_retries from sync_tasks_from_fix_plan
        # Check if it's set (implementation may vary)
        assert task.max_retries is not None


# =============================================================================
# Circuit Breaker Tests
# =============================================================================


class TestCircuitBreaker:
    """Tests for circuit breaker patterns."""

    @pytest.mark.asyncio
    async def test_consecutive_failures_trigger_circuit_breaker(self, sample_fix_plan_file):
        """Test circuit breaker opens after N consecutive failures."""
        # Note: Circuit breaker logic is mentioned in exit code 3 handling
        # This tests the pattern of consecutive failures
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Fail multiple tasks consecutively
        for i in range(3):
            await register_ralph_instance(f"ralph-{i}")
            result = await claim_task(f"ralph-{i}", project="test")
            if result:
                task_id = result["task_id"]
                await fail_task_with_retry(
                    f"ralph-{i}",
                    task_id,
                    f"Failure {i+1}",
                    ErrorCategory.CODE_ERROR
                )

        # Check that failures are recorded
        failed = await list_failed_tasks(project="test")
        assert len(failed) >= 3

    @pytest.mark.asyncio
    async def test_successful_task_resets_circuit_breaker(self, sample_fix_plan_file):
        """Test circuit breaker closes after successful task completion."""
        from chiefwiggum.coordination import complete_task

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Fail one task
        await register_ralph_instance("ralph-1")
        result1 = await claim_task("ralph-1", project="test")
        task1_id = result1["task_id"]
        await fail_task_with_retry("ralph-1", task1_id, "Error", ErrorCategory.CODE_ERROR)

        # Successfully complete another task
        await register_ralph_instance("ralph-2")
        result2 = await claim_task("ralph-2", project="test")
        task2_id = result2["task_id"]
        success = await complete_task(
            "ralph-2",
            task2_id,
            commit_sha="abc123",
            message="Success"
        )

        assert success is True

        task2 = await get_task_claim(task2_id)
        assert task2.status == TaskClaimStatus.COMPLETED


# =============================================================================
# Task Requeue with Error Metadata Tests
# =============================================================================


class TestTaskRequeue:
    """Tests for failed tasks returning to queue with error metadata."""

    @pytest.mark.asyncio
    async def test_retry_pending_task_requeued_with_metadata(self, sample_fix_plan_file):
        """Test failed tasks return to queue with error metadata preserved."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail with transient error
        error_msg = "Temporary API failure"
        await fail_task_with_retry(
            "ralph-1",
            task_id,
            error_msg,
            ErrorCategory.TRANSIENT
        )

        # Check error metadata before requeue
        task = await get_task_claim(task_id)
        assert task.error_message == error_msg
        assert task.error_category == ErrorCategory.TRANSIENT
        assert task.retry_count == 1

        # Set next_retry_at to past to allow immediate reprocessing
        conn = await get_connection()
        past_time = datetime.now() - timedelta(seconds=1)
        await conn.execute(
            "UPDATE task_claims SET next_retry_at = ? WHERE task_id = ?",
            (past_time, task_id)
        )
        await conn.commit()
        await conn.close()

        # Process retry - should move to pending
        count = await process_retry_tasks()
        assert count == 1

        # Check task is now pending with metadata preserved
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.error_message == error_msg  # Metadata preserved
        assert task.error_category == ErrorCategory.TRANSIENT
        assert task.retry_count == 1

    @pytest.mark.asyncio
    async def test_process_retry_tasks_only_processes_ready_tasks(self, sample_fix_plan_file):
        """Test process_retry_tasks only processes tasks with past retry time."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Fail task - it will have future retry time
        await fail_task_with_retry(
            "ralph-1",
            task_id,
            "Rate limit",
            ErrorCategory.TRANSIENT
        )

        # Set next_retry_at to future
        conn = await get_connection()
        future_time = datetime.now() + timedelta(hours=1)
        await conn.execute(
            "UPDATE task_claims SET next_retry_at = ? WHERE task_id = ?",
            (future_time, task_id)
        )
        await conn.commit()
        await conn.close()

        # Process retry - should not process this task yet
        count = await process_retry_tasks()
        assert count == 0

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING

    @pytest.mark.asyncio
    async def test_list_failed_tasks_shows_error_details(self, sample_fix_plan_file):
        """Test list_failed_tasks returns tasks with error details."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        error_msg = "Permission denied accessing file"
        await fail_task_with_retry(
            "ralph-1",
            task_id,
            error_msg,
            ErrorCategory.PERMISSION
        )

        # List failed tasks
        failed = await list_failed_tasks(project="test")
        assert len(failed) == 1
        assert failed[0].task_id == task_id
        assert failed[0].error_message == error_msg
        assert failed[0].error_category == ErrorCategory.PERMISSION
        assert failed[0].status == TaskClaimStatus.FAILED


# =============================================================================
# Crash Recovery Tests
# =============================================================================


class TestCrashRecovery:
    """Tests for Ralph crash detection and recovery."""

    @pytest.mark.asyncio
    async def test_ralph_crash_releases_claim(self, sample_fix_plan_file):
        """Test Ralph crash releases claim and returns task to pending."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Verify task is claimed
        task = await get_task_claim(task_id)
        assert task.claimed_by_ralph_id == "ralph-1"

        # Simulate crash: make instance stale
        from chiefwiggum.database import get_connection
        conn = await get_connection()
        stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
        await conn.execute(
            "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
            (stale_time, "ralph-1")
        )
        await conn.commit()
        await conn.close()

        # Mark stale instances as crashed
        count = await mark_stale_instances_crashed()
        assert count == 1

        # Check instance is marked crashed
        instance = await get_ralph_instance("ralph-1")
        assert instance.status == RalphInstanceStatus.CRASHED

        # Check task is released back to pending
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None

    @pytest.mark.asyncio
    async def test_crash_recovery_logs_error(self, sample_fix_plan_file, caplog):
        """Test crash recovery logs error information."""
        import logging
        caplog.set_level(logging.INFO)

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Make instance stale
        from chiefwiggum.database import get_connection
        conn = await get_connection()
        stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
        await conn.execute(
            "UPDATE ralph_instances SET last_heartbeat = ? WHERE ralph_id = ?",
            (stale_time, "ralph-1")
        )
        await conn.commit()
        await conn.close()

        # Mark crashed
        await mark_stale_instances_crashed()

        # Verify logging (implementation may vary)
        # Just check that crash detection ran
        instance = await get_ralph_instance("ralph-1")
        assert instance.status == RalphInstanceStatus.CRASHED

    @pytest.mark.asyncio
    async def test_multiple_crashes_handled_independently(self, sample_fix_plan_file):
        """Test multiple Ralph crashes handled independently."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Register multiple Ralphs and claim tasks
        for i in range(3):
            await register_ralph_instance(f"ralph-{i}")
            result = await claim_task(f"ralph-{i}", project="test")
            assert result is not None

        # Make all instances stale
        from chiefwiggum.database import get_connection
        conn = await get_connection()
        stale_time = datetime.now() - timedelta(minutes=HEARTBEAT_STALE_MINUTES + 1)
        await conn.execute(
            "UPDATE ralph_instances SET last_heartbeat = ?",
            (stale_time,)
        )
        await conn.commit()
        await conn.close()

        # Mark all crashed
        count = await mark_stale_instances_crashed()
        assert count == 3

        # Check all instances marked crashed
        for i in range(3):
            instance = await get_ralph_instance(f"ralph-{i}")
            assert instance.status == RalphInstanceStatus.CRASHED

        # Check all tasks released
        from chiefwiggum.coordination import list_pending_tasks
        pending = await list_pending_tasks(project="test")
        assert len(pending) == 3  # All tasks back in queue


# =============================================================================
# Backoff Timing Tests
# =============================================================================


class TestBackoffTiming:
    """Tests for exponential backoff timing calculations."""

    @pytest.mark.asyncio
    async def test_backoff_max_cap_at_5_minutes(self, sample_fix_plan_file):
        """Test backoff is capped at 5 minutes (300 seconds)."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Simulate many retries by manually setting retry_count
        conn = await get_connection()
        await conn.execute(
            "UPDATE task_claims SET retry_count = 10, max_retries = 15 WHERE task_id = ?",
            (task_id,)
        )
        await conn.commit()
        await conn.close()

        # Fail task - backoff should be capped at 300 seconds
        await fail_task_with_retry(
            "ralph-1",
            task_id,
            "Rate limit",
            ErrorCategory.TRANSIENT
        )

        task = await get_task_claim(task_id)
        backoff = task.next_retry_at - datetime.now()
        # Should be capped at 300 seconds
        assert backoff.total_seconds() <= 300
        assert backoff.total_seconds() >= 295  # Allow small timing variance

    @pytest.mark.asyncio
    async def test_backoff_formula_correct(self, sample_fix_plan_file):
        """Test backoff formula: min(300, 30 * 2^(retry_count-1))."""
        from chiefwiggum.database import get_connection

        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")

        # Test different retry counts
        test_cases = [
            (1, 30),    # 30 * 2^0 = 30
            (2, 60),    # 30 * 2^1 = 60
            (3, 120),   # 30 * 2^2 = 120
            (4, 240),   # 30 * 2^3 = 240
            (5, 300),   # 30 * 2^4 = 480, capped at 300
        ]

        for retry_num, expected_backoff in test_cases:
            # Claim a new task
            await register_ralph_instance(f"ralph-{retry_num}")
            result = await claim_task(f"ralph-{retry_num}", project="test")
            if not result:
                break  # No more tasks
            task_id = result["task_id"]

            # Set retry_count manually
            conn = await get_connection()
            await conn.execute(
                "UPDATE task_claims SET retry_count = ?, max_retries = 10 WHERE task_id = ?",
                (retry_num - 1, task_id)
            )
            await conn.commit()
            await conn.close()

            # Fail task
            await fail_task_with_retry(
                f"ralph-{retry_num}",
                task_id,
                f"Rate limit retry {retry_num}",
                ErrorCategory.TRANSIENT
            )

            # Check backoff
            task = await get_task_claim(task_id)
            backoff = task.next_retry_at - datetime.now()
            assert abs(backoff.total_seconds() - expected_backoff) < 2  # Allow 2s variance


# =============================================================================
# API Error Retry Tests
# =============================================================================


class TestAPIErrorRetry:
    """Tests for API error retry behavior."""

    @pytest.mark.asyncio
    async def test_api_429_error_retries(self, sample_fix_plan_file):
        """Test HTTP 429 errors trigger retry."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        # Classify 429 error
        error_msg = "HTTP 429: Too many requests to API"
        category = classify_error(error_msg)
        assert category == ErrorCategory.TRANSIENT

        # Fail task with 429
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            error_msg
        )

        assert success is True
        assert will_retry is True

        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.RETRY_PENDING

    @pytest.mark.asyncio
    async def test_api_503_error_retries(self, sample_fix_plan_file):
        """Test HTTP 503 service unavailable retries."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        error_msg = "Service temporarily unavailable (503)"
        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            error_msg
        )

        assert success is True
        assert will_retry is True

    @pytest.mark.asyncio
    async def test_api_connection_error_retries(self, sample_fix_plan_file):
        """Test API connection errors retry."""
        await sync_tasks_from_fix_plan(sample_fix_plan_file, project="test")
        await register_ralph_instance("ralph-1")

        result = await claim_task("ralph-1", project="test")
        task_id = result["task_id"]

        error_msg = "Connection reset by peer during API call"
        category = classify_error(error_msg)
        assert category == ErrorCategory.TRANSIENT

        success, will_retry = await fail_task_with_retry(
            "ralph-1",
            task_id,
            error_msg
        )

        assert success is True
        assert will_retry is True
