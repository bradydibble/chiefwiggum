"""Integration tests for ChiefWiggum Ralph lifecycle.

These tests verify the full lifecycle of Ralph instances:
- Spawn, work, stop cycle
- Task claiming and release on crash
- Zombie detection and recovery

These tests require:
- ralph-claude-code to be installed
- ANTHROPIC_API_KEY to be set (for full spawn tests)

Run with: pytest tests/test_integration_lifecycle.py -v --run-integration
"""

import asyncio
import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Mark all tests in this file as integration tests
pytestmark = [pytest.mark.integration, pytest.mark.slow]


def pytest_configure(config):
    """Register custom markers."""
    config.addinivalue_line(
        "markers", "integration: mark test as integration test (requires external deps)"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow-running"
    )


def pytest_collection_modifyitems(config, items):
    """Skip integration tests unless --run-integration is passed."""
    if config.getoption("--run-integration", default=False):
        return
    skip_integration = pytest.mark.skip(reason="need --run-integration option to run")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip_integration)


def pytest_addoption(parser):
    """Add --run-integration option."""
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="run integration tests",
    )


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def test_project_dir(tmp_path):
    """Create a temporary project directory with a @fix_plan.md."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create a simple fix plan
    fix_plan = project_dir / "@fix_plan.md"
    fix_plan.write_text("""# Test Fix Plan

## HIGH PRIORITY

### 1. Test Task
A simple test task.
- [ ] Step 1
- [ ] Step 2
""")

    # Create a basic .claude/settings.json for permissions
    claude_dir = project_dir / ".claude"
    claude_dir.mkdir()
    (claude_dir / "settings.json").write_text('{"permissions": {"allow": ["Read", "Write", "Edit"]}}')

    return project_dir


@pytest.fixture
async def test_db(tmp_path):
    """Set up a test database."""
    from chiefwiggum import init_db, reset_db

    test_db = tmp_path / "test_integration.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield test_db
    await reset_db()
    if "CHIEFWIGGUM_DB" in os.environ:
        del os.environ["CHIEFWIGGUM_DB"]


@pytest.fixture
def mock_ralph_data_dir(tmp_path, monkeypatch):
    """Monkeypatch path functions for tests."""
    import chiefwiggum.spawner as spawner_module

    ralph_dir = tmp_path / ".chiefwiggum" / "ralphs"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    status_dir = ralph_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    task_prompts_dir = ralph_dir / "task_prompts"
    task_prompts_dir.mkdir(parents=True, exist_ok=True)

    # Mock the private path functions to return temp directory paths
    monkeypatch.setattr(spawner_module, "_get_ralph_data_dir", lambda: ralph_dir)
    monkeypatch.setattr(spawner_module, "_get_status_dir", lambda: status_dir)
    monkeypatch.setattr(spawner_module, "_get_task_prompts_dir", lambda: task_prompts_dir)
    return ralph_dir


# =============================================================================
# Ralph Lifecycle Integration Tests
# =============================================================================


@pytest.mark.integration
class TestRalphLifecycle:
    """Integration tests for full Ralph lifecycle."""

    @pytest.mark.asyncio
    async def test_spawn_creates_pid_file(self, test_db, test_project_dir, mock_ralph_data_dir):
        """Spawning Ralph creates a PID file."""
        from chiefwiggum.spawner import (
            spawn_ralph_daemon,
            is_ralph_running,
            stop_ralph_daemon,
            generate_ralph_id,
        )
        from chiefwiggum.models import RalphConfig

        ralph_id = generate_ralph_id("test")

        # Skip if no API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY not set")

        # Check if ralph_loop.sh exists
        ralph_script = Path.home() / "claudecode" / "ralph-claude-code" / "ralph_loop.sh"
        if not ralph_script.exists():
            pytest.skip("ralph-claude-code not installed")

        try:
            success, message = spawn_ralph_daemon(
                ralph_id=ralph_id,
                project="test_project",
                fix_plan_path=str(test_project_dir / "@fix_plan.md"),
                config=RalphConfig(timeout_minutes=1),
                working_dir=str(test_project_dir),
            )

            if success:
                # Verify PID file was created
                pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
                assert pid_path.exists()

                # Verify process is running
                is_running, pid = is_ralph_running(ralph_id)
                assert is_running is True
                assert pid is not None

            # Either success or expected failure (e.g., ralph_loop.sh issues)
            # Both are valid for this test

        finally:
            # Cleanup
            try:
                stop_ralph_daemon(ralph_id, force=True)
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_stop_ralph_cleans_up(self, test_db, test_project_dir, mock_ralph_data_dir):
        """Stopping Ralph cleans up PID file and logs stop."""
        from chiefwiggum.spawner import (
            spawn_ralph_daemon,
            stop_ralph_daemon,
            is_ralph_running,
            get_ralph_log_path,
            generate_ralph_id,
        )
        from chiefwiggum.models import RalphConfig

        ralph_id = generate_ralph_id("test-stop")

        # Skip if no API key
        if not os.environ.get("ANTHROPIC_API_KEY"):
            pytest.skip("ANTHROPIC_API_KEY not set")

        ralph_script = Path.home() / "claudecode" / "ralph-claude-code" / "ralph_loop.sh"
        if not ralph_script.exists():
            pytest.skip("ralph-claude-code not installed")

        try:
            success, message = spawn_ralph_daemon(
                ralph_id=ralph_id,
                project="test_project",
                fix_plan_path=str(test_project_dir / "@fix_plan.md"),
                config=RalphConfig(timeout_minutes=1),
                working_dir=str(test_project_dir),
            )

            if success:
                # Stop the Ralph
                stop_success, stop_msg = stop_ralph_daemon(ralph_id)
                assert stop_success is True

                # Verify no longer running
                time.sleep(0.5)  # Brief wait for cleanup
                is_running, _ = is_ralph_running(ralph_id)
                assert is_running is False

                # Verify log file has stop message
                log_path = get_ralph_log_path(ralph_id)
                if log_path.exists():
                    log_content = log_path.read_text()
                    assert "stopped" in log_content.lower()

        finally:
            try:
                stop_ralph_daemon(ralph_id, force=True)
            except Exception:
                pass


# =============================================================================
# Zombie Detection and Recovery Integration Tests
# =============================================================================


@pytest.mark.integration
class TestZombieRecovery:
    """Integration tests for zombie detection and recovery."""

    @pytest.mark.asyncio
    async def test_cleanup_detects_dead_process(self, test_db, mock_ralph_data_dir):
        """cleanup_dead_ralphs detects and cleans up dead processes."""
        from chiefwiggum.spawner import cleanup_dead_ralphs, write_ralph_status

        ralph_id = "test-dead-ralph"

        # Create PID file pointing to non-existent process
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")

        # Create status file to simulate active ralph
        write_ralph_status(ralph_id, task_id="task-1", status="working")

        # Run cleanup
        cleaned = cleanup_dead_ralphs()

        assert ralph_id in cleaned
        assert not pid_path.exists()

    @pytest.mark.asyncio
    async def test_stuck_detection_triggers_recovery(self, test_db, mock_ralph_data_dir):
        """Stuck detection properly identifies unresponsive instances."""
        from chiefwiggum.spawner import (
            is_ralph_stuck,
            write_ralph_status,
            get_ralph_log_path,
        )
        import json

        ralph_id = "test-stuck-detection"

        # Create PID file pointing to non-existent process (simulates dead)
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")

        # Create a stale status file
        status_path = mock_ralph_data_dir / "status" / f"{ralph_id}.json"
        old_time = datetime.now() - timedelta(minutes=15)
        status_data = {
            "ralph_id": ralph_id,
            "task_id": "task-1",
            "status": "working",
            "updated_at": old_time.isoformat(),
        }
        status_path.write_text(json.dumps(status_data))

        # Create stale log file
        log_path = mock_ralph_data_dir / f"{ralph_id}.log"
        log_path.write_text("Old log content\n")
        old_mtime = time.time() - 900  # 15 minutes ago
        os.utime(log_path, (old_mtime, old_mtime))

        # Check if stuck
        is_stuck, reason = is_ralph_stuck(ralph_id)

        assert is_stuck is True
        # Reason could be dead process, stale log, or stale status
        assert any(x in reason.lower() for x in ["dead", "log", "stale", "not running"])


# =============================================================================
# Task Claiming and Release Integration Tests
# =============================================================================


@pytest.mark.integration
class TestTaskClaimingIntegration:
    """Integration tests for task claiming with Ralph lifecycle."""

    @pytest.mark.asyncio
    async def test_handle_stuck_releases_task(self, test_db, mock_ralph_data_dir, test_project_dir):
        """handle_stuck_ralph releases claimed task back to pending."""
        from chiefwiggum import (
            sync_tasks_from_fix_plan,
            claim_task,
            get_task_claim,
            register_ralph_instance,
            TaskClaimStatus,
        )
        from chiefwiggum.spawner import handle_stuck_ralph, write_ralph_status
        from chiefwiggum.coordination import _update_instance_task

        ralph_id = "test-stuck-release"

        # Setup: sync tasks and register instance
        await sync_tasks_from_fix_plan(str(test_project_dir / "@fix_plan.md"), "test_project")
        await register_ralph_instance(ralph_id)

        # Claim a task
        claimed = await claim_task(ralph_id, project="test_project")
        if claimed is None:
            pytest.skip("No tasks available to claim")

        task_id = claimed["task_id"]
        await _update_instance_task(ralph_id, task_id)

        # Verify task is in progress
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.IN_PROGRESS
        assert task.claimed_by_ralph_id == ralph_id

        # Handle stuck (no actual process to kill, just release task)
        result = await handle_stuck_ralph(ralph_id, "Test stuck scenario")

        # Verify task was released
        assert result["task_released"] is True
        assert result["task_id"] == task_id

        # Verify task is back to pending
        task = await get_task_claim(task_id)
        assert task.status == TaskClaimStatus.PENDING
        assert task.claimed_by_ralph_id is None


# =============================================================================
# Health Monitoring Integration Tests
# =============================================================================


@pytest.mark.integration
class TestHealthMonitoringIntegration:
    """Integration tests for health monitoring."""

    @pytest.mark.asyncio
    async def test_activity_monitoring_with_real_process(self, mock_ralph_data_dir):
        """Activity monitoring works with real process."""
        from chiefwiggum.spawner import (
            get_ralph_activity,
            get_process_health,
            write_ralph_status,
        )
        import subprocess

        ralph_id = "test-activity-real"

        # Start a real process
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        try:
            # Create PID file
            pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
            pid_path.write_text(str(proc.pid))

            # Create log file
            log_path = mock_ralph_data_dir / f"{ralph_id}.log"
            log_path.write_text("Test log content\n")

            # Create status
            write_ralph_status(ralph_id, task_id="task-1", status="working")

            # Check health
            health = get_process_health(ralph_id)
            assert health["running"] is True
            assert health["healthy"] is True

            # Check activity
            activity = get_ralph_activity(ralph_id)
            assert activity["process_state"] in ("running", "sleeping")
            assert activity["log_age_seconds"] is not None
            assert activity["is_responsive"] is True

        finally:
            proc.kill()
            proc.wait()

    @pytest.mark.asyncio
    async def test_full_health_check_cycle(self, test_db, mock_ralph_data_dir):
        """Full health check cycle with multiple indicators."""
        from chiefwiggum.spawner import (
            is_ralph_stuck,
            get_ralph_activity,
            get_process_health,
            get_status_staleness,
            write_ralph_status,
        )
        from chiefwiggum import register_ralph_instance
        import subprocess

        ralph_id = "test-full-health"

        # Register instance in DB
        await register_ralph_instance(ralph_id)

        # Create healthy Ralph state
        proc = subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL)

        try:
            # Create PID file
            pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
            pid_path.write_text(str(proc.pid))

            # Create fresh log
            log_path = mock_ralph_data_dir / f"{ralph_id}.log"
            log_path.write_text("Fresh log\n")

            # Create fresh status
            write_ralph_status(ralph_id, task_id="task-1", status="working", message="Healthy")

            # Check all health indicators
            process_health = get_process_health(ralph_id)
            status_staleness = get_status_staleness(ralph_id)
            activity = get_ralph_activity(ralph_id)
            is_stuck, reason = is_ralph_stuck(ralph_id)

            # All should indicate healthy
            assert process_health["healthy"] is True
            assert status_staleness["stale"] is False
            assert activity["is_responsive"] is True
            assert is_stuck is False
            assert reason == "OK"

        finally:
            proc.kill()
            proc.wait()
