"""Tests for ChiefWiggum Ralph process lifecycle and health monitoring.

Tests cover:
- Process health detection (running, zombie, stopped, dead)
- Ralph activity monitoring (log file, status file freshness)
- Stuck detection and recovery
- Zombie reaping
- Process spawn/stop lifecycle
"""

import os
import signal
import subprocess
import time
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from chiefwiggum.spawner import (
    get_process_health,
    get_status_staleness,
    get_ralph_activity,
    is_ralph_stuck,
    reap_zombie_ralph,
    cleanup_dead_ralphs,
    write_ralph_status,
    read_ralph_status,
    is_ralph_running,
    get_ralph_status_path,
)


# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def test_ralph_dir(tmp_path):
    """Create a temporary Ralph data directory."""
    ralph_dir = tmp_path / ".chiefwiggum" / "ralphs"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    status_dir = ralph_dir / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    return ralph_dir


@pytest.fixture
def mock_ralph_data_dir(test_ralph_dir, monkeypatch):
    """Monkeypatch path functions to use temp directory."""
    import chiefwiggum.spawner as spawner_module
    # Mock the private path functions to return temp directory paths
    monkeypatch.setattr(spawner_module, "_get_ralph_data_dir", lambda: test_ralph_dir)
    monkeypatch.setattr(spawner_module, "_get_task_prompts_dir", lambda: test_ralph_dir / "task_prompts")
    monkeypatch.setattr(spawner_module, "_get_status_dir", lambda: test_ralph_dir / "status")
    return test_ralph_dir


@pytest.fixture
def running_process():
    """Create a running process for testing."""
    proc = subprocess.Popen(
        ["sleep", "60"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    yield proc
    # Cleanup
    try:
        proc.kill()
        proc.wait()
    except Exception:
        pass


# =============================================================================
# Process Health Detection Tests
# =============================================================================


class TestProcessHealth:
    """Tests for get_process_health()"""

    def test_detects_running_process(self, mock_ralph_data_dir, running_process):
        """Running process returns healthy state."""
        ralph_id = "test-running"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        health = get_process_health(ralph_id)

        assert health["running"] is True
        assert health["pid"] == running_process.pid
        assert health["state"] in ("running", "sleeping")
        assert health["healthy"] is True

    def test_detects_nonexistent_process(self, mock_ralph_data_dir):
        """Non-existent PID returns not found."""
        ralph_id = "test-nonexistent"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")  # Very unlikely to exist

        health = get_process_health(ralph_id)

        assert health["running"] is False
        assert health["state"] == "dead"
        assert health["healthy"] is False

    def test_no_pid_file(self, mock_ralph_data_dir):
        """No PID file returns appropriate state."""
        ralph_id = "test-no-pid"

        health = get_process_health(ralph_id)

        assert health["running"] is False
        assert health["pid"] is None
        assert health["message"] == "No PID file"

    def test_detects_stopped_process(self, mock_ralph_data_dir, running_process):
        """SIGSTOP'd process returns 'stopped' state."""
        ralph_id = "test-stopped"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Stop the process
        os.kill(running_process.pid, signal.SIGSTOP)
        time.sleep(0.2)

        try:
            health = get_process_health(ralph_id)
            assert health["state"] == "stopped"
            assert health["healthy"] is False
        finally:
            # Resume it so cleanup works
            os.kill(running_process.pid, signal.SIGCONT)

    def test_invalid_pid_in_file(self, mock_ralph_data_dir):
        """Invalid PID in file is handled gracefully."""
        ralph_id = "test-invalid-pid"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("not-a-number")

        health = get_process_health(ralph_id)

        assert health["running"] is False
        assert health["pid"] is None
        assert "Invalid PID file" in health["message"]


# =============================================================================
# Status Staleness Tests
# =============================================================================


class TestStatusStaleness:
    """Tests for get_status_staleness()"""

    def test_fresh_status_file(self, mock_ralph_data_dir):
        """Recently updated status file is not stale."""
        ralph_id = "test-fresh"
        write_ralph_status(ralph_id, task_id="task-1", status="working", message="Active")

        staleness = get_status_staleness(ralph_id)

        assert staleness["exists"] is True
        assert staleness["stale"] is False
        assert staleness["age_seconds"] is not None
        assert staleness["age_seconds"] < 5

    def test_stale_status_file(self, mock_ralph_data_dir):
        """Old status file is marked as stale."""
        import json

        ralph_id = "test-stale"
        status_path = get_ralph_status_path(ralph_id)

        # Write status with old timestamp
        old_time = datetime.now() - timedelta(minutes=10)
        status_data = {
            "ralph_id": ralph_id,
            "task_id": "task-1",
            "status": "working",
            "updated_at": old_time.isoformat(),
        }
        status_path.write_text(json.dumps(status_data))

        staleness = get_status_staleness(ralph_id)

        assert staleness["exists"] is True
        assert staleness["stale"] is True
        assert staleness["age_seconds"] > 500  # About 10 minutes

    def test_no_status_file(self, mock_ralph_data_dir):
        """Missing status file is reported."""
        ralph_id = "test-no-status"

        staleness = get_status_staleness(ralph_id)

        assert staleness["exists"] is False
        assert staleness["stale"] is True
        assert "No status file" in staleness["message"]


# =============================================================================
# Ralph Activity Tests
# =============================================================================


class TestRalphActivity:
    """Tests for get_ralph_activity()"""

    def test_active_ralph_with_recent_log(self, mock_ralph_data_dir, running_process):
        """Active Ralph with recent log activity is responsive."""
        ralph_id = "test-active"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Create log file
        log_path = mock_ralph_data_dir / f"{ralph_id}.log"
        log_path.write_text("Log line 1\nLog line 2\n")

        # Create status file
        write_ralph_status(ralph_id, task_id="task-1", status="working", message="Active")

        activity = get_ralph_activity(ralph_id)

        assert activity["log_age_seconds"] is not None
        assert activity["log_age_seconds"] < 5
        assert activity["status_age_seconds"] is not None
        assert activity["process_state"] in ("running", "sleeping")
        assert activity["is_responsive"] is True

    def test_ralph_with_no_log_file(self, mock_ralph_data_dir, running_process):
        """Ralph with no log file has None log age."""
        ralph_id = "test-no-log"

        # Create PID file only
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        activity = get_ralph_activity(ralph_id)

        assert activity["log_age_seconds"] is None
        assert activity["log_size"] is None

    def test_ralph_with_stale_log(self, mock_ralph_data_dir, running_process):
        """Ralph with stale log file shows age correctly."""
        ralph_id = "test-stale-log"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Create log file and set old mtime
        log_path = mock_ralph_data_dir / f"{ralph_id}.log"
        log_path.write_text("Old log content\n")

        # Set modification time to 10 minutes ago
        old_time = time.time() - 600
        os.utime(log_path, (old_time, old_time))

        activity = get_ralph_activity(ralph_id)

        assert activity["log_age_seconds"] is not None
        assert activity["log_age_seconds"] > 500  # About 10 minutes
        assert activity["is_responsive"] is False  # Stale log = not responsive


# =============================================================================
# Stuck Detection Tests
# =============================================================================


class TestStuckDetection:
    """Tests for is_ralph_stuck()"""

    def test_healthy_ralph_not_stuck(self, mock_ralph_data_dir, running_process):
        """Healthy running Ralph is not stuck."""
        ralph_id = "test-healthy"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Create fresh log
        log_path = mock_ralph_data_dir / f"{ralph_id}.log"
        log_path.write_text("Fresh log content\n")

        # Create fresh status
        write_ralph_status(ralph_id, task_id="task-1", status="working", message="Active")

        is_stuck, reason = is_ralph_stuck(ralph_id)

        assert is_stuck is False
        assert reason == "OK"

    def test_dead_process_is_stuck(self, mock_ralph_data_dir):
        """Dead process is detected as stuck."""
        ralph_id = "test-dead"

        # Create PID file pointing to non-existent process
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")

        is_stuck, reason = is_ralph_stuck(ralph_id)

        assert is_stuck is True
        assert "DEAD" in reason or "not running" in reason.lower()

    def test_stopped_process_is_stuck(self, mock_ralph_data_dir, running_process):
        """Stopped (SIGSTOP'd) process is detected as stuck."""
        ralph_id = "test-stopped"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Stop the process
        os.kill(running_process.pid, signal.SIGSTOP)
        time.sleep(0.2)

        try:
            is_stuck, reason = is_ralph_stuck(ralph_id)
            assert is_stuck is True
            assert "STOPPED" in reason
        finally:
            os.kill(running_process.pid, signal.SIGCONT)

    def test_stale_log_is_stuck(self, mock_ralph_data_dir, running_process):
        """Ralph with very stale log is detected as stuck."""
        ralph_id = "test-stale"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Create log file with old mtime (10 minutes)
        log_path = mock_ralph_data_dir / f"{ralph_id}.log"
        log_path.write_text("Old log content\n")
        old_time = time.time() - 600
        os.utime(log_path, (old_time, old_time))

        is_stuck, reason = is_ralph_stuck(ralph_id)

        assert is_stuck is True
        assert "log activity" in reason.lower() or "No log activity" in reason

    def test_not_running_ralph_not_stuck(self, mock_ralph_data_dir):
        """Ralph that was never started is not stuck."""
        ralph_id = "test-never-started"
        # No PID file exists

        is_stuck, reason = is_ralph_stuck(ralph_id)

        assert is_stuck is False
        assert "Not running" in reason


# =============================================================================
# Zombie Reaping Tests
# =============================================================================


class TestZombieReaping:
    """Tests for reap_zombie_ralph() and cleanup_dead_ralphs()"""

    def test_reap_non_zombie_returns_false(self, mock_ralph_data_dir, running_process):
        """Trying to reap a non-zombie process returns False."""
        ralph_id = "test-not-zombie"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        result = reap_zombie_ralph(ralph_id)

        assert result is False

    def test_cleanup_dead_ralphs_removes_stale_pids(self, mock_ralph_data_dir):
        """cleanup_dead_ralphs removes PID files for dead processes."""
        ralph_id = "test-dead-ralph"

        # Create PID file pointing to non-existent process
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")

        # Mock _ensure_data_dir to return our test directory
        with patch("chiefwiggum.spawner._ensure_data_dir", return_value=mock_ralph_data_dir):
            # Run cleanup
            cleaned = cleanup_dead_ralphs()

        assert ralph_id in cleaned
        # PID file should be removed
        assert not pid_path.exists()


# =============================================================================
# Status Read/Write Tests
# =============================================================================


class TestStatusReadWrite:
    """Tests for write_ralph_status() and read_ralph_status()"""

    def test_write_and_read_status(self, mock_ralph_data_dir):
        """Status can be written and read back."""
        ralph_id = "test-status"

        write_ralph_status(
            ralph_id,
            task_id="task-123",
            status="working",
            loop_count=5,
            message="Processing task",
        )

        status = read_ralph_status(ralph_id)

        assert status is not None
        assert status["ralph_id"] == ralph_id
        assert status["task_id"] == "task-123"
        assert status["status"] == "working"
        assert status["loop_count"] == 5
        assert status["message"] == "Processing task"
        assert "updated_at" in status

    def test_read_nonexistent_status(self, mock_ralph_data_dir):
        """Reading non-existent status returns None."""
        ralph_id = "test-no-status"

        status = read_ralph_status(ralph_id)

        assert status is None

    def test_status_timestamp_format(self, mock_ralph_data_dir):
        """Status timestamp is valid ISO format."""
        ralph_id = "test-timestamp"

        write_ralph_status(ralph_id, task_id=None, status="idle")

        status = read_ralph_status(ralph_id)
        # Should be parseable as ISO datetime
        parsed = datetime.fromisoformat(status["updated_at"])
        assert isinstance(parsed, datetime)


# =============================================================================
# Is Ralph Running Tests
# =============================================================================


class TestIsRalphRunning:
    """Tests for is_ralph_running()"""

    def test_running_ralph(self, mock_ralph_data_dir, running_process):
        """Running Ralph is detected."""
        ralph_id = "test-running"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        is_running, pid = is_ralph_running(ralph_id)

        assert is_running is True
        assert pid == running_process.pid

    def test_not_running_ralph(self, mock_ralph_data_dir):
        """Non-running Ralph is detected."""
        ralph_id = "test-not-running"
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text("99999999")

        is_running, pid = is_ralph_running(ralph_id)

        assert is_running is False

    def test_no_pid_file(self, mock_ralph_data_dir):
        """Ralph with no PID file is not running."""
        ralph_id = "test-no-pid"

        is_running, pid = is_ralph_running(ralph_id)

        assert is_running is False
        assert pid is None


# =============================================================================
# Handle Stuck Ralph Tests (async)
# =============================================================================


class TestHandleStuckRalph:
    """Tests for handle_stuck_ralph() - requires async"""

    @pytest.mark.asyncio
    async def test_handle_stuck_terminates_process(self, mock_ralph_data_dir, running_process):
        """handle_stuck_ralph terminates the stuck process."""
        from chiefwiggum.spawner import handle_stuck_ralph
        from unittest.mock import AsyncMock

        ralph_id = "test-stuck"

        # Create PID file
        pid_path = mock_ralph_data_dir / f"{ralph_id}.pid"
        pid_path.write_text(str(running_process.pid))

        # Mock the coordination functions since we don't have DB
        # These are imported inside handle_stuck_ralph, so patch them at the coordination module
        mock_get_instance = AsyncMock(return_value=None)
        mock_release_claim = AsyncMock()
        mock_update_status = AsyncMock()

        with patch("chiefwiggum.coordination.get_ralph_instance", mock_get_instance):
            with patch("chiefwiggum.coordination.release_claim", mock_release_claim):
                with patch("chiefwiggum.coordination.update_instance_status", mock_update_status):
                    result = await handle_stuck_ralph(ralph_id, "Test stuck reason")

        assert result["terminated"] is True
        assert "Test stuck reason" in result["message"] or ralph_id in result["message"]

    @pytest.mark.asyncio
    async def test_handle_stuck_writes_status(self, mock_ralph_data_dir):
        """handle_stuck_ralph writes crashed status."""
        from chiefwiggum.spawner import handle_stuck_ralph
        from unittest.mock import AsyncMock

        ralph_id = "test-stuck-status"

        # No running process, just test status writing
        mock_get_instance = AsyncMock(return_value=None)
        mock_update_status = AsyncMock()

        with patch("chiefwiggum.coordination.get_ralph_instance", mock_get_instance):
            with patch("chiefwiggum.coordination.update_instance_status", mock_update_status):
                _result = await handle_stuck_ralph(ralph_id, "Test reason")

        # Should have written status file
        status = read_ralph_status(ralph_id)
        assert status is not None
        assert status["status"] == "crashed"
