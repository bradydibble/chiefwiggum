"""Tests for @fix_plan.md update functionality.

This module tests the critical file update logic that marks tasks as complete
in the @fix_plan.md file using checkmarks, while handling file locking to
prevent concurrent write conflicts.
"""

import fcntl
import tempfile
from pathlib import Path
import pytest
import threading
import time

from chiefwiggum.fix_plan_writer import (
    update_task_completion_marker,
    check_task_marked_complete,
    create_backup,
)


@pytest.fixture
def sample_fix_plan(tmp_path):
    """Create a sample @fix_plan.md file for testing."""
    fix_plan = tmp_path / "@fix_plan.md"
    content = """# Fix Plan

## HIGH Priority

### 22. File Processing Workflow
- Implement file upload
- Process files in background

### 23. API Integration
- Connect to external API
- Handle rate limiting

## MEDIUM Priority

### 24. User Authentication
- Login form
- Session management

#### PF-1: Database Migration
- Create migration script
- Run on staging

#### Plain Task Title Without Number
- Just a regular task
- With some details
"""
    fix_plan.write_text(content)
    return fix_plan


class TestTaskCompletionMarker:
    """Tests for update_task_completion_marker() function."""

    def test_update_task_completion_marker_adds_checkmark(self, sample_fix_plan):
        """Test that update_task_completion_marker() adds ✓ checkmark."""
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22-file-processing",
            task_number=22,
            mark_complete=True
        )

        assert success is True

        content = sample_fix_plan.read_text()
        assert "### 22. File Processing Workflow ✓" in content
        assert "### 23. API Integration\n" in content  # Unchanged

    def test_update_task_completion_marker_removes_checkmark(self, sample_fix_plan):
        """Test that update_task_completion_marker() can remove checkmark."""
        # First, add a checkmark
        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=True
        )

        # Verify it's there
        content = sample_fix_plan.read_text()
        assert "### 22. File Processing Workflow ✓" in content

        # Now remove it
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=False
        )

        assert success is True

        content = sample_fix_plan.read_text()
        assert "### 22. File Processing Workflow\n" in content
        assert "✓" not in content.split("### 22. File Processing Workflow")[1].split("\n")[0]

    def test_update_task_completion_marker_id_based_format(self, sample_fix_plan):
        """Test updating ID-based task format (#### PF-1: Title)."""
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="PF-1",
            mark_complete=True
        )

        assert success is True

        content = sample_fix_plan.read_text()
        assert "#### PF-1: Database Migration ✓" in content

    def test_update_task_completion_marker_plain_format(self, sample_fix_plan):
        """Test updating plain task format (#### Plain Title)."""
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="Plain Task Title Without Number",
            mark_complete=True
        )

        assert success is True

        content = sample_fix_plan.read_text()
        assert "#### Plain Task Title Without Number ✓" in content

    def test_update_task_completion_marker_task_not_found(self, sample_fix_plan):
        """Test handling when task doesn't exist in fix_plan.md."""
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-999-nonexistent",
            task_number=999,
            mark_complete=True
        )

        assert success is False

        # File should be unchanged
        content = sample_fix_plan.read_text()
        assert "999" not in content

    def test_update_task_completion_marker_file_not_found(self, tmp_path):
        """Test handling when @fix_plan.md doesn't exist."""
        nonexistent = tmp_path / "nonexistent.md"

        success = update_task_completion_marker(
            fix_plan_path=nonexistent,
            task_id="task-1",
            task_number=1,
            mark_complete=True
        )

        assert success is False

    def test_update_task_completion_marker_already_marked(self, sample_fix_plan):
        """Test that marking an already-complete task is idempotent."""
        # Mark complete twice
        success1 = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=True
        )

        success2 = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=True
        )

        assert success1 is True
        assert success2 is True

        # Should still have exactly one checkmark
        content = sample_fix_plan.read_text()
        assert content.count("### 22. File Processing Workflow ✓") == 1

    def test_update_task_completion_marker_handles_line_endings(self, tmp_path):
        """Test that line endings are normalized to LF."""
        fix_plan = tmp_path / "@fix_plan.md"

        # Create file with CRLF line endings
        content = "### 1. Task One\r\n### 2. Task Two\r\n"
        fix_plan.write_bytes(content.encode('utf-8'))

        update_task_completion_marker(
            fix_plan_path=fix_plan,
            task_id="task-1",
            task_number=1,
            mark_complete=True
        )

        # File should be updated successfully
        result_content = fix_plan.read_text()
        assert "### 1. Task One ✓" in result_content
        assert "### 2. Task Two" in result_content


class TestConcurrentUpdates:
    """Tests for file locking and concurrent update handling."""

    def test_update_task_completion_marker_concurrent_updates(self, sample_fix_plan):
        """Test that concurrent updates are handled via file locking."""
        # Hold a lock on the file
        with open(sample_fix_plan, 'r+') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)

            # Try to update from another "process" (should fail due to lock)
            success = update_task_completion_marker(
                fix_plan_path=sample_fix_plan,
                task_id="task-22",
                task_number=22,
                mark_complete=True
            )

            # Should fail because lock is held
            assert success is False

            # Release lock
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)

        # Now try again without lock - should succeed
        success = update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=True
        )

        assert success is True

    def test_update_task_completion_marker_threaded_updates(self, sample_fix_plan):
        """Test that file locking prevents corruption during concurrent updates.

        Due to file-level locking, some threads may fail to acquire the lock.
        This is correct behavior - it prevents file corruption. Failed updates
        should be retried by the caller.
        """
        results = []

        def update_task(task_num):
            # Retry up to 3 times if lock is held
            for attempt in range(3):
                success = update_task_completion_marker(
                    fix_plan_path=sample_fix_plan,
                    task_id=f"task-{task_num}",
                    task_number=task_num,
                    mark_complete=True
                )
                if success:
                    results.append((task_num, True))
                    return
                time.sleep(0.01)  # Brief wait before retry

            results.append((task_num, False))

        # Create threads to update different tasks
        threads = [
            threading.Thread(target=update_task, args=(22,)),
            threading.Thread(target=update_task, args=(23,)),
            threading.Thread(target=update_task, args=(24,)),
        ]

        # Start all threads
        for t in threads:
            t.start()

        # Wait for completion
        for t in threads:
            t.join()

        # With retries, all updates should eventually succeed
        assert len(results) == 3
        assert all(success for _, success in results)

        # Verify all tasks marked complete
        content = sample_fix_plan.read_text()
        assert "### 22. File Processing Workflow ✓" in content
        assert "### 23. API Integration ✓" in content
        assert "### 24. User Authentication ✓" in content


class TestCheckTaskMarkedComplete:
    """Tests for check_task_marked_complete() function."""

    def test_check_task_marked_complete_returns_true(self, sample_fix_plan):
        """Test that check_task_marked_complete() detects completed tasks."""
        # Mark task complete
        update_task_completion_marker(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22,
            mark_complete=True
        )

        # Check it's marked
        is_complete = check_task_marked_complete(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22
        )

        assert is_complete is True

    def test_check_task_marked_complete_returns_false(self, sample_fix_plan):
        """Test that check_task_marked_complete() returns False for incomplete tasks."""
        is_complete = check_task_marked_complete(
            fix_plan_path=sample_fix_plan,
            task_id="task-22",
            task_number=22
        )

        assert is_complete is False

    def test_check_task_marked_complete_task_not_found(self, sample_fix_plan):
        """Test that check_task_marked_complete() returns False for nonexistent tasks."""
        is_complete = check_task_marked_complete(
            fix_plan_path=sample_fix_plan,
            task_id="task-999",
            task_number=999
        )

        assert is_complete is False


class TestCreateBackup:
    """Tests for create_backup() function."""

    def test_create_backup_success(self, sample_fix_plan):
        """Test that create_backup() creates a backup file."""
        backup_path = create_backup(sample_fix_plan)

        assert backup_path is not None
        assert backup_path.exists()
        assert backup_path.suffix == ".backup"
        assert backup_path.stem.endswith(".md")

        # Verify backup content matches original
        assert backup_path.read_text() == sample_fix_plan.read_text()

    def test_create_backup_file_not_found(self, tmp_path):
        """Test that create_backup() handles nonexistent files."""
        nonexistent = tmp_path / "nonexistent.md"

        backup_path = create_backup(nonexistent)

        assert backup_path is None
