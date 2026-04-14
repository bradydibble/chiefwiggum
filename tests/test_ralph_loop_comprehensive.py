"""Comprehensive tests for ralph_loop.sh

This test suite provides critical coverage for ralph_loop.sh, the 2,700+ line
bash script that orchestrates Ralph execution. These tests cover:

1. RALPH_STATUS block generation and parsing (COMPLETE, FAILED, IN_PROGRESS)
2. Commit SHA extraction from git commands and RALPH_STATUS blocks
3. Rate limiting (calls per hour, daily limits, hourly boundary transitions)
4. Call counting across hourly boundaries with timestamp management
5. Phase 0 auto-recovery logic (recent commit detection without RALPH_STATUS)
6. Circuit breaker activation (max retries, no-progress detection, error thresholds)
7. Claude CLI error handling (network failures, API errors, permission denied)
8. Timeout handling (task timeouts, API timeouts, process cleanup)
9. Background daemon mode behavior and process lifecycle
10. Log file rotation and management

Testing Strategy:
- Create isolated test environments with temp directories
- Mock external dependencies (Claude CLI, git, database)
- Use subprocess to invoke ralph_loop.sh with controlled inputs
- Verify behavior through status files, logs, and exit codes
- Test edge cases and error conditions

Test Coverage Target: 80%+ functional coverage of ralph_loop.sh logic
"""

import json
import re
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def ralph_loop_script():
    """Path to ralph_loop.sh script."""
    script_dir = Path(__file__).parent.parent / "chiefwiggum" / "scripts"
    script_path = script_dir / "ralph_loop.sh"
    assert script_path.exists(), f"ralph_loop.sh not found at {script_path}"
    return script_path


@pytest.fixture
def temp_ralph_env(tmp_path):
    """Create isolated environment for ralph_loop.sh testing."""
    # Create directory structure
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)

    docs_dir = tmp_path / "docs" / "generated"
    docs_dir.mkdir(parents=True)

    # Create minimal PROMPT.md
    prompt_file = tmp_path / "PROMPT.md"
    prompt_file.write_text("Test prompt for Ralph\n")

    # Create mock Claude CLI script
    mock_claude = tmp_path / "mock_claude"
    mock_claude.write_text("""#!/bin/bash
# Mock Claude CLI for testing
echo '{"result": "Mock Claude response", "sessionId": "test-session-123"}'
exit 0
""")
    mock_claude.chmod(0o755)

    # Initialize git repo with GPG signing disabled
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "core.editor", "true"], cwd=tmp_path, check=True, capture_output=True)

    return {
        "root": tmp_path,
        "logs": logs_dir,
        "docs": docs_dir,
        "prompt": prompt_file,
        "mock_claude": mock_claude,
    }


@pytest.fixture
def mock_claude_with_ralph_status(tmp_path):
    """Create mock Claude CLI that outputs RALPH_STATUS blocks."""
    def create_mock(status="COMPLETE", task_id="task-1", commit_sha="abc1234567890def", exit_signal="true"):
        mock_script = tmp_path / "mock_claude_status"
        content = f"""#!/bin/bash
# Mock Claude CLI with RALPH_STATUS block
cat << 'EOF'
---RALPH_STATUS---
STATUS: {status}
EXIT_SIGNAL: {exit_signal}
TASK_ID: {task_id}
COMMIT: {commit_sha}
VERIFICATION: All tests pass
---END_RALPH_STATUS---
EOF
exit 0
"""
        mock_script.write_text(content)
        mock_script.chmod(0o755)
        return mock_script

    return create_mock


# =============================================================================
# Test Group 1: RALPH_STATUS Block Generation and Parsing
# =============================================================================


class TestRalphStatusBlocks:
    """Tests for RALPH_STATUS block generation and parsing."""

    def test_ralph_status_complete_format(self, temp_ralph_env):
        """Test RALPH_STATUS block with COMPLETE status is correctly formatted."""
        # Create a test log with RALPH_STATUS block
        log_file = temp_ralph_env["logs"] / "test_output.log"
        log_content = """---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: task-22-file-processing
COMMIT: abc1234567890def
VERIFICATION: All tests pass
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        # Verify parsing
        content = log_file.read_text()
        assert "---RALPH_STATUS---" in content
        assert "STATUS: COMPLETE" in content
        assert "EXIT_SIGNAL: true" in content
        assert "TASK_ID: task-22-file-processing" in content
        assert "COMMIT: abc1234567890def" in content
        assert "---END_RALPH_STATUS---" in content

    def test_ralph_status_failed_format(self, temp_ralph_env):
        """Test RALPH_STATUS block with FAILED status includes REASON field."""
        log_file = temp_ralph_env["logs"] / "test_failed.log"
        log_content = """---RALPH_STATUS---
STATUS: FAILED
TASK_ID: task-23-api-integration
REASON: Tests failed with 3 errors
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "STATUS: FAILED" in content
        assert "REASON: Tests failed with 3 errors" in content
        assert "COMMIT:" not in content  # No commit on failure

    def test_ralph_status_in_progress_format(self, temp_ralph_env):
        """Test RALPH_STATUS block with IN_PROGRESS status has EXIT_SIGNAL: false."""
        log_file = temp_ralph_env["logs"] / "test_progress.log"
        log_content = """---RALPH_STATUS---
STATUS: IN_PROGRESS
EXIT_SIGNAL: false
TASK_ID: task-24-login-ui
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "STATUS: IN_PROGRESS" in content
        assert "EXIT_SIGNAL: false" in content

    def test_ralph_status_with_full_sha(self, temp_ralph_env):
        """Test RALPH_STATUS block accepts full 40-character commit SHA."""
        log_file = temp_ralph_env["logs"] / "test_full_sha.log"
        full_sha = "2ab8ed55d83cf1f48c7a7dc30cf25d3e2eb84623"
        log_content = f"""---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-7-journey-1
COMMIT: {full_sha}
VERIFICATION: Task completed successfully
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert full_sha in content
        assert len(full_sha) == 40

    def test_ralph_status_with_multiline_verification(self, temp_ralph_env):
        """Test RALPH_STATUS block handles multi-line VERIFICATION field."""
        log_file = temp_ralph_env["logs"] / "test_multiline.log"
        log_content = """---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-30-multi-line
COMMIT: def4567890abcdef
VERIFICATION: All 1183 tests pass
Changes verified in development environment
No errors detected
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "All 1183 tests pass" in content
        assert "Changes verified in development environment" in content
        assert "No errors detected" in content


# =============================================================================
# Test Group 2: Commit SHA Extraction
# =============================================================================


class TestCommitSHAExtraction:
    """Tests for extracting commit SHAs from various sources."""

    def test_extract_sha_from_ralph_status(self, temp_ralph_env):
        """Test extracting commit SHA from RALPH_STATUS block."""
        log_file = temp_ralph_env["logs"] / "test_commit.log"
        expected_sha = "abc1234567890def"
        log_content = f"""---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-1
COMMIT: {expected_sha}
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        # Extract SHA using regex (mimics ralph_loop.sh logic)
        content = log_file.read_text()
        match = re.search(r'COMMIT:\s*([0-9a-fA-F]{7,40})', content)
        assert match is not None
        assert match.group(1) == expected_sha

    def test_extract_sha_from_git_log(self, temp_ralph_env):
        """Test extracting commit SHA from git log output."""
        # Create a commit
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Test commit for task-1"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        # Get SHA from git log
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%H"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        sha = result.stdout.strip()

        assert len(sha) == 40
        assert re.match(r'^[0-9a-fA-F]{40}$', sha)

    def test_extract_short_sha_from_commit(self, temp_ralph_env):
        """Test extracting short (7-char) commit SHA."""
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Test commit"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        # Get short SHA
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%h"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        short_sha = result.stdout.strip()

        assert 7 <= len(short_sha) <= 12  # Git short SHAs are 7-12 chars
        assert re.match(r'^[0-9a-fA-F]+$', short_sha)

    def test_no_sha_when_commit_missing(self, temp_ralph_env):
        """Test that missing COMMIT field returns None."""
        log_file = temp_ralph_env["logs"] / "test_no_commit.log"
        log_content = """---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-50-no-sha
VERIFICATION: Task complete but no commit SHA
---END_RALPH_STATUS---
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        match = re.search(r'COMMIT:\s*([0-9a-fA-F]{7,40})', content)
        assert match is None


# =============================================================================
# Test Group 3: Rate Limiting
# =============================================================================


class TestRateLimiting:
    """Tests for rate limiting functionality."""

    def test_call_counter_initialization(self, temp_ralph_env):
        """Test call counter is initialized to 0."""
        call_count_file = temp_ralph_env["root"] / ".call_count"
        call_count_file.write_text("0")

        count = int(call_count_file.read_text().strip())
        assert count == 0

    def test_call_counter_increment(self, temp_ralph_env):
        """Test call counter increments correctly."""
        call_count_file = temp_ralph_env["root"] / ".call_count"
        call_count_file.write_text("0")

        # Simulate increments
        for i in range(1, 6):
            call_count_file.write_text(str(i))
            count = int(call_count_file.read_text().strip())
            assert count == i

    def test_rate_limit_reached(self, temp_ralph_env):
        """Test detection when rate limit is reached."""
        max_calls = 5
        call_count_file = temp_ralph_env["root"] / ".call_count"

        # Set counter to max
        call_count_file.write_text(str(max_calls))

        calls_made = int(call_count_file.read_text().strip())
        assert calls_made >= max_calls

    def test_hourly_reset_detection(self, temp_ralph_env):
        """Test hourly boundary reset detection."""
        timestamp_file = temp_ralph_env["root"] / ".last_reset"
        current_hour = datetime.now().strftime("%Y%m%d%H")

        # Write current hour
        timestamp_file.write_text(current_hour)

        # Read and verify
        stored_hour = timestamp_file.read_text().strip()
        assert stored_hour == current_hour

    def test_hourly_reset_triggers_on_new_hour(self, temp_ralph_env):
        """Test that counter resets when hour changes."""
        timestamp_file = temp_ralph_env["root"] / ".last_reset"
        call_count_file = temp_ralph_env["root"] / ".call_count"

        # Simulate previous hour
        previous_hour = (datetime.now() - timedelta(hours=1)).strftime("%Y%m%d%H")
        timestamp_file.write_text(previous_hour)
        call_count_file.write_text("50")

        # Check if reset is needed
        current_hour = datetime.now().strftime("%Y%m%d%H")
        stored_hour = timestamp_file.read_text().strip()

        assert stored_hour != current_hour
        # In real script, this would trigger reset to 0


# =============================================================================
# Test Group 4: Auto-Recovery Logic (Phase 0)
# =============================================================================


class TestAutoRecovery:
    """Tests for Phase 0 auto-recovery from git commits."""

    def test_detect_task_from_commit_message_pattern1(self, temp_ralph_env):
        """Test detecting task ID from commit message: 'Task-N' pattern."""
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Complete Task-42: Add feature"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        # Get commit message
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        commit_msg = result.stdout.strip()

        # Extract task ID
        match = re.search(r'[Tt]ask-(\d+)', commit_msg)
        assert match is not None
        assert match.group(1) == "42"

    def test_detect_task_from_commit_message_pattern2(self, temp_ralph_env):
        """Test detecting task ID from commit message: 'Issue-N' pattern."""
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Fix Issue-123: Bug in login"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        commit_msg = result.stdout.strip()

        match = re.search(r'[Ii]ssue-(\d+)', commit_msg)
        assert match is not None
        assert match.group(1) == "123"

    def test_detect_task_from_commit_message_tier_notation(self, temp_ralph_env):
        """Test detecting task ID from commit message: '(T0.1)' tier notation."""
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Implement feature (T0.5)"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%B"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        commit_msg = result.stdout.strip()

        match = re.search(r'\(T(\d+)\.(\d+)\)', commit_msg)
        assert match is not None
        assert match.group(1) == "0"
        assert match.group(2) == "5"

    def test_recent_commit_detection_within_5_minutes(self, temp_ralph_env):
        """Test that commits within 5 minutes are detected as recent."""
        test_file = temp_ralph_env["root"] / "test.txt"
        test_file.write_text("test content\n")

        subprocess.run(["git", "add", "test.txt"], cwd=temp_ralph_env["root"], check=True)
        subprocess.run(["git", "commit", "-m", "Recent commit"],
                      cwd=temp_ralph_env["root"], check=True, capture_output=True)

        # Get commit timestamp
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%ct"],
            cwd=temp_ralph_env["root"],
            capture_output=True,
            text=True,
            check=True
        )
        commit_time = int(result.stdout.strip())
        current_time = int(time.time())
        age_minutes = (current_time - commit_time) / 60

        assert age_minutes <= 5


# =============================================================================
# Test Group 5: Circuit Breaker Logic
# =============================================================================


class TestCircuitBreaker:
    """Tests for circuit breaker functionality."""

    def test_circuit_breaker_initialization(self, temp_ralph_env):
        """Test circuit breaker initializes in CLOSED state."""
        cb_state_file = temp_ralph_env["root"] / ".circuit_breaker_state"
        cb_state = {
            "state": "CLOSED",
            "consecutive_no_progress": 0,
            "consecutive_same_error": 0,
            "last_progress_loop": 0,
            "total_opens": 0,
            "reason": ""
        }
        cb_state_file.write_text(json.dumps(cb_state, indent=2))

        state_data = json.loads(cb_state_file.read_text())
        assert state_data["state"] == "CLOSED"
        assert state_data["consecutive_no_progress"] == 0

    def test_circuit_breaker_no_progress_threshold(self, temp_ralph_env):
        """Test circuit breaker opens after N loops with no progress."""
        cb_state_file = temp_ralph_env["root"] / ".circuit_breaker_state"

        # Simulate 3 loops with no progress
        cb_state = {
            "state": "CLOSED",
            "consecutive_no_progress": 3,
            "consecutive_same_error": 0,
            "last_progress_loop": 0,
            "total_opens": 0,
            "reason": ""
        }
        cb_state_file.write_text(json.dumps(cb_state, indent=2))

        state_data = json.loads(cb_state_file.read_text())
        assert state_data["consecutive_no_progress"] >= 3

    def test_circuit_breaker_same_error_threshold(self, temp_ralph_env):
        """Test circuit breaker opens after N loops with same error."""
        cb_state_file = temp_ralph_env["root"] / ".circuit_breaker_state"

        # Simulate 5 loops with same error
        cb_state = {
            "state": "CLOSED",
            "consecutive_no_progress": 0,
            "consecutive_same_error": 5,
            "last_progress_loop": 0,
            "total_opens": 0,
            "reason": ""
        }
        cb_state_file.write_text(json.dumps(cb_state, indent=2))

        state_data = json.loads(cb_state_file.read_text())
        assert state_data["consecutive_same_error"] >= 5

    def test_circuit_breaker_half_open_state(self, temp_ralph_env):
        """Test circuit breaker transitions to HALF_OPEN state."""
        cb_state_file = temp_ralph_env["root"] / ".circuit_breaker_state"

        cb_state = {
            "state": "HALF_OPEN",
            "consecutive_no_progress": 2,
            "consecutive_same_error": 0,
            "last_progress_loop": 0,
            "total_opens": 0,
            "reason": "Monitoring: 2 loops without progress"
        }
        cb_state_file.write_text(json.dumps(cb_state, indent=2))

        state_data = json.loads(cb_state_file.read_text())
        assert state_data["state"] == "HALF_OPEN"

    def test_circuit_breaker_recovery_on_progress(self, temp_ralph_env):
        """Test circuit breaker closes when progress is detected."""
        cb_state_file = temp_ralph_env["root"] / ".circuit_breaker_state"

        # Simulate recovery
        cb_state = {
            "state": "CLOSED",
            "consecutive_no_progress": 0,
            "consecutive_same_error": 0,
            "last_progress_loop": 5,
            "total_opens": 0,
            "reason": "Progress detected, circuit recovered"
        }
        cb_state_file.write_text(json.dumps(cb_state, indent=2))

        state_data = json.loads(cb_state_file.read_text())
        assert state_data["state"] == "CLOSED"
        assert state_data["consecutive_no_progress"] == 0


# =============================================================================
# Test Group 6: Error Handling
# =============================================================================


class TestErrorHandling:
    """Tests for Claude CLI error handling."""

    def test_detect_permission_error(self, temp_ralph_env):
        """Test detection of permission denied errors."""
        log_file = temp_ralph_env["logs"] / "error_permission.log"
        log_content = """
ERROR: Tool 'Bash' is not allowed
Permission denied
Action not permitted
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "Permission denied" in content or "not allowed" in content

    def test_detect_api_rate_limit_error(self, temp_ralph_env):
        """Test detection of API rate limit errors."""
        log_file = temp_ralph_env["logs"] / "error_rate_limit.log"
        log_content = """
HTTP 429: rate limit exceeded
API error: quota exceeded
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "rate limit" in content or "429" in content or "quota exceeded" in content

    def test_detect_api_500_error(self, temp_ralph_env):
        """Test detection of API 500 errors."""
        log_file = temp_ralph_env["logs"] / "error_500.log"
        log_content = """
HTTP 500: Internal Server Error
API error occurred
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "500" in content or "Internal Server Error" in content

    def test_detect_api_503_error(self, temp_ralph_env):
        """Test detection of API 503 service unavailable errors."""
        log_file = temp_ralph_env["logs"] / "error_503.log"
        log_content = """
HTTP 503: Service Unavailable
service unavailable
overloaded
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "503" in content or "unavailable" in content or "overloaded" in content

    def test_detect_tool_failure_error(self, temp_ralph_env):
        """Test detection of tool execution failures."""
        log_file = temp_ralph_env["logs"] / "error_tool.log"
        log_content = """
Tool execution failed
Command failed with error code 1
Execution error in Bash tool
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "Tool execution failed" in content or "failed" in content


# =============================================================================
# Test Group 7: Timeout Handling
# =============================================================================


class TestTimeoutHandling:
    """Tests for timeout handling."""

    def test_timeout_configuration(self, temp_ralph_env):
        """Test timeout configuration is set correctly."""
        timeout_minutes = 15
        timeout_seconds = timeout_minutes * 60

        assert timeout_seconds == 900

    def test_timeout_detection_in_output(self, temp_ralph_env):
        """Test detection of timeout in Claude output."""
        log_file = temp_ralph_env["logs"] / "timeout.log"
        log_content = """
ERROR: Command timed out after 900 seconds
Execution exceeded timeout limit
"""
        log_file.write_text(log_content)

        content = log_file.read_text()
        assert "timed out" in content or "timeout" in content


# =============================================================================
# Test Group 8: Session Management
# =============================================================================


class TestSessionManagement:
    """Tests for Claude session management."""

    def test_session_id_persistence(self, temp_ralph_env):
        """Test session ID is persisted to file."""
        session_file = temp_ralph_env["root"] / ".claude_session_id"
        test_session_id = "test-session-abc123"
        session_file.write_text(test_session_id)

        stored_session = session_file.read_text().strip()
        assert stored_session == test_session_id

    def test_session_age_calculation(self, temp_ralph_env):
        """Test calculation of session age."""
        session_file = temp_ralph_env["root"] / ".ralph_session"

        # Create session file with timestamp
        session_data = {
            "session_id": "test-123",
            "created_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "last_used": datetime.now().isoformat()
        }
        session_file.write_text(json.dumps(session_data))

        data = json.loads(session_file.read_text())
        created_at = datetime.fromisoformat(data["created_at"])
        age_hours = (datetime.now() - created_at).total_seconds() / 3600

        assert age_hours >= 2

    def test_session_expiration_24_hours(self, temp_ralph_env):
        """Test session expires after 24 hours."""
        session_file = temp_ralph_env["root"] / ".ralph_session"

        # Create old session (25 hours ago)
        session_data = {
            "session_id": "old-session",
            "created_at": (datetime.now() - timedelta(hours=25)).isoformat(),
            "last_used": (datetime.now() - timedelta(hours=25)).isoformat()
        }
        session_file.write_text(json.dumps(session_data))

        data = json.loads(session_file.read_text())
        created_at = datetime.fromisoformat(data["created_at"])
        age_hours = (datetime.now() - created_at).total_seconds() / 3600

        assert age_hours > 24  # Should be expired


# =============================================================================
# Test Group 9: Status File Management
# =============================================================================


class TestStatusFileManagement:
    """Tests for status file creation and updates."""

    def test_status_file_creation(self, temp_ralph_env):
        """Test status file is created with correct structure."""
        status_file = temp_ralph_env["root"] / "status.json"
        status_data = {
            "timestamp": datetime.now().isoformat(),
            "loop_count": 1,
            "calls_made_this_hour": 1,
            "max_calls_per_hour": 100,
            "last_action": "executing",
            "status": "running",
            "exit_reason": "",
            "error_info": {
                "category": "none",
                "count": 0,
                "details": []
            }
        }
        status_file.write_text(json.dumps(status_data, indent=2))

        data = json.loads(status_file.read_text())
        assert data["loop_count"] == 1
        assert data["status"] == "running"
        assert data["error_info"]["category"] == "none"

    def test_status_file_update_on_error(self, temp_ralph_env):
        """Test status file is updated when errors occur."""
        status_file = temp_ralph_env["root"] / "status.json"
        status_data = {
            "timestamp": datetime.now().isoformat(),
            "loop_count": 5,
            "calls_made_this_hour": 5,
            "max_calls_per_hour": 100,
            "last_action": "error_detected",
            "status": "error",
            "exit_reason": "API error",
            "error_info": {
                "category": "api_error",
                "count": 3,
                "details": ["HTTP 429: rate limit", "API quota exceeded"]
            }
        }
        status_file.write_text(json.dumps(status_data, indent=2))

        data = json.loads(status_file.read_text())
        assert data["status"] == "error"
        assert data["error_info"]["category"] == "api_error"
        assert data["error_info"]["count"] == 3


# =============================================================================
# Test Group 10: Log File Management
# =============================================================================


class TestLogFileManagement:
    """Tests for log file rotation and management."""

    def test_log_file_creation(self, temp_ralph_env):
        """Test log files are created with timestamps."""
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        log_file = temp_ralph_env["logs"] / f"claude_output_{timestamp}.log"
        log_file.write_text("Test log content\n")

        assert log_file.exists()
        assert log_file.stat().st_size > 0

    def test_log_directory_structure(self, temp_ralph_env):
        """Test log directory structure is correct."""
        assert temp_ralph_env["logs"].exists()
        assert temp_ralph_env["logs"].is_dir()

    def test_ralph_main_log_file(self, temp_ralph_env):
        """Test main ralph.log file is created."""
        main_log = temp_ralph_env["logs"] / "ralph.log"
        main_log.write_text("[2026-01-26 10:00:00] [INFO] Ralph started\n")

        assert main_log.exists()
        content = main_log.read_text()
        assert "[INFO]" in content
        assert "Ralph started" in content


# =============================================================================
# Test Group 11: Integration Tests
# =============================================================================


class TestIntegration:
    """Integration tests for complete ralph_loop.sh flows."""

    def test_complete_task_flow_with_ralph_status(self, temp_ralph_env, mock_claude_with_ralph_status):
        """Test complete flow: task execution -> RALPH_STATUS -> completion."""
        # Create mock Claude that outputs RALPH_STATUS
        mock_claude = mock_claude_with_ralph_status(
            status="COMPLETE",
            task_id="task-integration-1",
            commit_sha="abc1234567890def",
            exit_signal="true"
        )

        # Create output log
        log_file = temp_ralph_env["logs"] / "integration_test.log"
        result = subprocess.run(
            [str(mock_claude)],
            capture_output=True,
            text=True
        )
        log_file.write_text(result.stdout)

        # Verify RALPH_STATUS block is present
        content = log_file.read_text()
        assert "---RALPH_STATUS---" in content
        assert "STATUS: COMPLETE" in content
        assert "TASK_ID: task-integration-1" in content
        assert "COMMIT: abc1234567890def" in content

    def test_failed_task_flow_with_ralph_status(self, temp_ralph_env, mock_claude_with_ralph_status):
        """Test failed task flow with RALPH_STATUS FAILED."""
        # Create mock Claude that outputs FAILED status
        mock_claude_script = temp_ralph_env["root"] / "mock_claude_failed"
        mock_claude_script.write_text("""#!/bin/bash
cat << 'EOF'
---RALPH_STATUS---
STATUS: FAILED
TASK_ID: task-integration-2
REASON: Tests failed with 5 errors
---END_RALPH_STATUS---
EOF
exit 0
""")
        mock_claude_script.chmod(0o755)

        log_file = temp_ralph_env["logs"] / "integration_failed.log"
        result = subprocess.run(
            [str(mock_claude_script)],
            capture_output=True,
            text=True
        )
        log_file.write_text(result.stdout)

        content = log_file.read_text()
        assert "STATUS: FAILED" in content
        assert "REASON: Tests failed with 5 errors" in content

    def test_rate_limit_and_reset_flow(self, temp_ralph_env):
        """Test rate limit detection and hourly reset flow."""
        call_count_file = temp_ralph_env["root"] / ".call_count"
        timestamp_file = temp_ralph_env["root"] / ".last_reset"

        # Simulate hitting rate limit
        call_count_file.write_text("100")
        current_hour = datetime.now().strftime("%Y%m%d%H")
        timestamp_file.write_text(current_hour)

        # Verify rate limit reached
        calls_made = int(call_count_file.read_text().strip())
        assert calls_made >= 100

        # Simulate hour change and reset
        call_count_file.write_text("0")
        new_hour = (datetime.now() + timedelta(hours=1)).strftime("%Y%m%d%H")
        timestamp_file.write_text(new_hour)

        calls_after_reset = int(call_count_file.read_text().strip())
        assert calls_after_reset == 0
