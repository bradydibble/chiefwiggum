"""Tests for RALPH_STATUS block parsing and task completion detection.

This module tests the critical parsing logic that detects when Ralph has
completed a task by analyzing the RALPH_STATUS block in Ralph's log output.
"""

from pathlib import Path

import pytest

from chiefwiggum.spawner import check_task_completion


@pytest.fixture
def temp_ralph_log(monkeypatch, tmp_path):
    """Create a temporary Ralph log file for testing."""
    def mock_get_ralph_log_path(ralph_id: str) -> Path:
        return tmp_path / f"{ralph_id}.log"

    monkeypatch.setattr("chiefwiggum.spawner.get_ralph_log_path", mock_get_ralph_log_path)
    return tmp_path


class TestRalphStatusParsing:
    """Tests for RALPH_STATUS block parsing."""

    def test_parse_ralph_status_complete(self, temp_ralph_log):
        """Test parsing COMPLETE status from RALPH_STATUS block."""
        ralph_id = "test-ralph-001"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
[2026-01-22 17:47:12] Claude output received
---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: task-22-file-processing
COMMIT: abc1234567890def
VERIFICATION: All tests pass
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-22-file-processing"
        assert failure is None
        assert commit == "abc1234567890def"

    def test_parse_ralph_status_complete_with_full_sha(self, temp_ralph_log):
        """Test parsing with full 40-character commit SHA."""
        ralph_id = "test-ralph-002"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-7-journey-1
COMMIT: 2ab8ed55d83cf1f48c7a7dc30cf25d3e2eb84623
VERIFICATION: Task completed successfully
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-7-journey-1"
        assert failure is None
        assert commit == "2ab8ed55d83cf1f48c7a7dc30cf25d3e2eb84623"

    def test_parse_ralph_status_failed(self, temp_ralph_log):
        """Test parsing FAILED status from RALPH_STATUS block."""
        ralph_id = "test-ralph-003"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: FAILED
TASK_ID: task-23-api-integration
REASON: Tests failed with 3 errors
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-23-api-integration"
        assert failure == "Tests failed with 3 errors"
        assert commit is None

    def test_parse_ralph_status_in_progress(self, temp_ralph_log):
        """Test that IN_PROGRESS status doesn't signal completion."""
        ralph_id = "test-ralph-004"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: IN_PROGRESS
EXIT_SIGNAL: false
TASK_ID: task-24-login-ui
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id is None
        assert failure is None
        assert commit is None

    def test_parse_ralph_status_malformed_no_task_id(self, temp_ralph_log):
        """Test handling of malformed RALPH_STATUS block (missing TASK_ID)."""
        ralph_id = "test-ralph-005"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: COMPLETE
COMMIT: abc123
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        # Should fail gracefully - no task_id match means no completion detected
        assert task_id is None
        assert failure is None
        assert commit is None

    def test_parse_ralph_status_with_multiline_verification(self, temp_ralph_log):
        """Test parsing with multi-line VERIFICATION field."""
        ralph_id = "test-ralph-006"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-30-multi-line
COMMIT: def4567890abcdef
VERIFICATION: All 1183 tests pass
Changes verified in development environment
No errors detected
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-30-multi-line"
        assert failure is None
        assert commit == "def4567890abcdef"

    def test_parse_ralph_status_multiple_blocks_uses_first(self, temp_ralph_log):
        """Test that when multiple RALPH_STATUS blocks exist, the first COMPLETE is used.

        NOTE: Current implementation uses re.search() which finds the FIRST match.
        For completion detection, we might want to find the LAST (most recent) match instead.
        This could be improved by using re.finditer() and taking the last match.
        """
        ralph_id = "test-ralph-007"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
[2026-01-22 17:00:00] Completed
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-25-first
COMMIT: abcdef1234567
---END_RALPH_STATUS---

[2026-01-22 17:30:00] In progress on next task
---RALPH_STATUS---
STATUS: IN_PROGRESS
TASK_ID: task-25-second
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        # Current behavior: uses FIRST occurrence
        assert task_id == "task-25-first"
        assert failure is None
        assert commit == "abcdef1234567"


class TestLegacyFormatParsing:
    """Tests for legacy TASK_COMPLETE format parsing."""

    def test_parse_legacy_task_complete(self, temp_ralph_log):
        """Test fallback to legacy TASK_COMPLETE format."""
        ralph_id = "test-ralph-legacy-001"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
[2026-01-22 16:44:41] Task completed
TASK_COMPLETE: task-5-issue-5
COMMIT: 60af9ebcbca3b926d491ee8bdf1d35e3d91bdb67
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-5-issue-5"
        assert failure is None
        assert commit == "60af9ebcbca3b926d491ee8bdf1d35e3d91bdb67"

    def test_parse_legacy_task_complete_no_commit(self, temp_ralph_log):
        """Test legacy format without COMMIT field."""
        ralph_id = "test-ralph-legacy-002"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
TASK_COMPLETE: task-10-no-commit
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-10-no-commit"
        assert failure is None
        assert commit is None

    def test_parse_legacy_task_failed(self, temp_ralph_log):
        """Test legacy TASK_FAILED format."""
        ralph_id = "test-ralph-legacy-003"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
TASK_FAILED: task-12-broken
REASON: Dependencies not installed
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-12-broken"
        assert failure == "Dependencies not installed"
        assert commit is None


class TestEdgeCases:
    """Tests for edge cases and error handling."""

    def test_check_task_completion_no_marker(self, temp_ralph_log):
        """Test check_task_completion() returns None when no marker found."""
        ralph_id = "test-ralph-edge-001"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
[2026-01-22 17:00:00] Ralph working on task...
[2026-01-22 17:05:00] Still working...
[2026-01-22 17:10:00] Making progress...
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id is None
        assert failure is None
        assert commit is None

    def test_check_task_completion_log_does_not_exist(self, temp_ralph_log):
        """Test check_task_completion() handles missing log file gracefully."""
        ralph_id = "test-ralph-nonexistent"

        # Don't create the log file

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id is None
        assert failure is None
        assert commit is None

    def test_check_task_completion_empty_log(self, temp_ralph_log):
        """Test check_task_completion() handles empty log file."""
        ralph_id = "test-ralph-empty"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_path.write_text("")

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id is None
        assert failure is None
        assert commit is None

    def test_check_task_completion_malformed_commit_sha(self, temp_ralph_log):
        """Test handling of malformed commit SHA (too short)."""
        ralph_id = "test-ralph-bad-sha"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-99-bad-sha
COMMIT: abc123
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        # Regex requires at least 7 hex chars, "abc123" is only 6
        assert task_id == "task-99-bad-sha"
        assert failure is None
        assert commit is None  # Invalid SHA should not be captured

    def test_check_task_completion_reads_last_50kb(self, temp_ralph_log):
        """Test that check_task_completion() only reads last 50KB of log."""
        ralph_id = "test-ralph-large-log"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        # Create a log larger than 50KB
        # Write 60KB of junk, then the completion marker
        junk = "X" * 60000  # 60KB of junk
        log_content = f"""{junk}
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-large-log
COMMIT: 1234567890abcdef
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        # Should find the marker even in large log
        assert task_id == "task-large-log"
        assert failure is None
        assert commit == "1234567890abcdef"

    def test_ralph_status_with_extra_fields(self, temp_ralph_log):
        """Test that extra fields in RALPH_STATUS don't break parsing."""
        ralph_id = "test-ralph-extra-fields"
        log_path = temp_ralph_log / f"{ralph_id}.log"

        log_content = """
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-50-extra
COMMIT: abcdef1234567890
EXTRA_FIELD: some value
ANOTHER_FIELD: another value
VERIFICATION: Tests pass
---END_RALPH_STATUS---
"""
        log_path.write_text(log_content)

        task_id, failure, commit = check_task_completion(ralph_id)

        assert task_id == "task-50-extra"
        assert failure is None
        assert commit == "abcdef1234567890"
