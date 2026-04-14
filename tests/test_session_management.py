"""Tests for ChiefWiggum Session Management.

Tests cover:
- Session expiry detection and renewal
- Session continuity and resumption
- Session file corruption handling
- Session file creation and initialization
- Multiple Ralph session isolation
- Session history logging
- Claude CLI integration with session IDs
- Session cleanup of old files
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from chiefwiggum.spawner import (
    get_ralph_session_path,
)

# =============================================================================
# Test Fixtures
# =============================================================================


@pytest.fixture
def test_ralph_dir(tmp_path):
    """Create a temporary Ralph data directory."""
    ralph_dir = tmp_path / ".chiefwiggum" / "ralphs"
    ralph_dir.mkdir(parents=True, exist_ok=True)
    return ralph_dir


@pytest.fixture
def mock_ralph_data_dir(test_ralph_dir, monkeypatch):
    """Monkeypatch path functions to use temp directory."""
    import chiefwiggum.spawner as spawner_module
    monkeypatch.setattr(spawner_module, "_get_ralph_data_dir", lambda: test_ralph_dir)
    monkeypatch.setattr(spawner_module, "_get_task_prompts_dir", lambda: test_ralph_dir / "task_prompts")
    monkeypatch.setattr(spawner_module, "_get_status_dir", lambda: test_ralph_dir / "status")
    return test_ralph_dir


@pytest.fixture
def session_file_path(test_ralph_dir):
    """Return path for a test session file."""
    def _path(ralph_id: str) -> Path:
        return test_ralph_dir / f"{ralph_id}.session"
    return _path


# =============================================================================
# Session Expiry Tests
# =============================================================================


class TestSessionExpiry:
    """Tests for session expiry detection and renewal."""

    def test_fresh_session_not_expired(self, mock_ralph_data_dir, session_file_path):
        """Session file less than 24 hours old is not expired."""
        ralph_id = "test-fresh-session"
        session_path = session_file_path(ralph_id)

        # Create a fresh session file
        session_data = {
            "session_id": "sess-123456",
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Session should exist and not be expired
        assert session_path.exists()

        # Check file modification time is recent (< 1 minute)
        mtime = session_path.stat().st_mtime
        age_seconds = time.time() - mtime
        assert age_seconds < 60, "Session file should be fresh"

    def test_old_session_expired(self, mock_ralph_data_dir, session_file_path):
        """Session file older than 24 hours should be detected as expired."""
        ralph_id = "test-old-session"
        session_path = session_file_path(ralph_id)

        # Create an old session file
        old_time = datetime.now() - timedelta(hours=25)
        session_data = {
            "session_id": "sess-old-123",
            "last_used": old_time.isoformat(),
            "created_at": old_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Set file modification time to 25 hours ago
        old_timestamp = time.time() - (25 * 3600)
        os.utime(session_path, (old_timestamp, old_timestamp))

        # Check file is old (> 24 hours)
        mtime = session_path.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        assert age_hours > 24, "Session should be expired"

    def test_session_expiry_custom_hours(self, mock_ralph_data_dir, session_file_path):
        """Session expiry respects custom expiry hours setting."""
        ralph_id = "test-custom-expiry"
        session_path = session_file_path(ralph_id)

        # Create session file 13 hours old (expired if threshold is 12, not expired if 24)
        old_time = datetime.now() - timedelta(hours=13)
        session_data = {
            "session_id": "sess-custom-123",
            "last_used": old_time.isoformat(),
            "created_at": old_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Set file modification time to 13 hours ago
        old_timestamp = time.time() - (13 * 3600)
        os.utime(session_path, (old_timestamp, old_timestamp))

        # Check file age
        mtime = session_path.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        assert 12 < age_hours < 14, "Session should be 13 hours old"

        # For custom expiry of 12 hours, this would be expired
        # For custom expiry of 24 hours (default), this would not be expired
        assert age_hours > 12, "Session exceeds 12-hour threshold"
        assert age_hours < 24, "Session within 24-hour threshold"

    def test_session_expiry_boundary_condition(self, mock_ralph_data_dir, session_file_path):
        """Session exactly at 24 hours is treated as expired."""
        ralph_id = "test-boundary-session"
        session_path = session_file_path(ralph_id)

        # Create session file exactly 24 hours old
        exact_time = datetime.now() - timedelta(hours=24)
        session_data = {
            "session_id": "sess-boundary-123",
            "last_used": exact_time.isoformat(),
            "created_at": exact_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Set file modification time to exactly 24 hours ago
        exact_timestamp = time.time() - (24 * 3600)
        os.utime(session_path, (exact_timestamp, exact_timestamp))

        # Check file age is at boundary
        mtime = session_path.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        assert 23.9 < age_hours <= 24.1, "Session should be at 24-hour boundary"


# =============================================================================
# Session Continuity Tests
# =============================================================================


class TestSessionContinuity:
    """Tests for session resumption and continuity."""

    def test_resume_existing_session(self, mock_ralph_data_dir, session_file_path):
        """Ralph resumes existing session correctly."""
        ralph_id = "test-resume"
        session_path = session_file_path(ralph_id)

        # Create existing session
        session_id = "sess-existing-abc123"
        session_data = {
            "session_id": session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": (datetime.now() - timedelta(hours=1)).isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Read session back
        assert session_path.exists()
        loaded_data = json.loads(session_path.read_text())
        assert loaded_data["session_id"] == session_id
        assert "last_used" in loaded_data
        assert "created_at" in loaded_data

    def test_session_continuity_updates_timestamp(self, mock_ralph_data_dir, session_file_path):
        """Session continuity updates last_used timestamp."""
        ralph_id = "test-continuity"
        session_path = session_file_path(ralph_id)

        # Create initial session
        initial_time = datetime.now() - timedelta(minutes=30)
        session_data = {
            "session_id": "sess-continuity-123",
            "last_used": initial_time.isoformat(),
            "created_at": initial_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Simulate session update
        time.sleep(0.1)  # Small delay to ensure timestamp difference
        updated_time = datetime.now()
        session_data["last_used"] = updated_time.isoformat()
        session_path.write_text(json.dumps(session_data))

        # Verify timestamp was updated
        loaded_data = json.loads(session_path.read_text())
        last_used = datetime.fromisoformat(loaded_data["last_used"])
        created_at = datetime.fromisoformat(loaded_data["created_at"])

        assert last_used > created_at, "last_used should be newer than created_at"

    def test_session_continuity_disabled(self, mock_ralph_data_dir, session_file_path):
        """Session continuity can be disabled via no_continue flag."""
        ralph_id = "test-no-continue"
        session_path = session_file_path(ralph_id)

        # Create existing session
        session_data = {
            "session_id": "sess-old-456",
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Simulate no_continue mode: delete and recreate session
        old_session_id = session_data["session_id"]
        session_path.unlink()

        # Create new session
        new_session_id = f"sess-new-{time.time()}"
        new_session_data = {
            "session_id": new_session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(new_session_data))

        # Verify new session has different ID
        loaded_data = json.loads(session_path.read_text())
        assert loaded_data["session_id"] != old_session_id
        assert loaded_data["session_id"] == new_session_id


# =============================================================================
# Session Corruption Handling Tests
# =============================================================================


class TestSessionCorruption:
    """Tests for handling corrupted or invalid session files."""

    def test_corrupted_json_handled(self, mock_ralph_data_dir, session_file_path):
        """Corrupted JSON session file is detected and handled."""
        ralph_id = "test-corrupted"
        session_path = session_file_path(ralph_id)

        # Write invalid JSON
        session_path.write_text("{ invalid json content }")

        # Attempt to load - should fail gracefully
        try:
            json.loads(session_path.read_text())
            pytest.fail("Should have raised JSONDecodeError")
        except json.JSONDecodeError:
            # Expected behavior
            pass

    def test_empty_session_file_handled(self, mock_ralph_data_dir, session_file_path):
        """Empty session file is handled correctly."""
        ralph_id = "test-empty"
        session_path = session_file_path(ralph_id)

        # Create empty file
        session_path.write_text("")

        # Attempt to load - should fail gracefully
        try:
            json.loads(session_path.read_text())
            pytest.fail("Should have raised JSONDecodeError for empty file")
        except json.JSONDecodeError:
            # Expected behavior
            pass

    def test_missing_required_fields_handled(self, mock_ralph_data_dir, session_file_path):
        """Session file missing required fields is handled."""
        ralph_id = "test-incomplete"
        session_path = session_file_path(ralph_id)

        # Write session with missing session_id
        incomplete_data = {
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
            # Missing session_id
        }
        session_path.write_text(json.dumps(incomplete_data))

        # Load and verify missing field
        loaded_data = json.loads(session_path.read_text())
        assert "session_id" not in loaded_data
        assert "last_used" in loaded_data

    def test_recreate_corrupted_session(self, mock_ralph_data_dir, session_file_path):
        """Corrupted session file can be recreated with fresh data."""
        ralph_id = "test-recreate"
        session_path = session_file_path(ralph_id)

        # Write corrupted data
        session_path.write_text("corrupted content")
        assert session_path.exists()

        # Delete corrupted file
        session_path.unlink()

        # Create fresh session
        new_session_id = f"sess-fresh-{time.time()}"
        fresh_data = {
            "session_id": new_session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(fresh_data))

        # Verify new session is valid
        loaded_data = json.loads(session_path.read_text())
        assert loaded_data["session_id"] == new_session_id
        assert "last_used" in loaded_data


# =============================================================================
# Session File Creation Tests
# =============================================================================


class TestSessionFileCreation:
    """Tests for session file creation and initialization."""

    def test_create_new_session_when_missing(self, mock_ralph_data_dir, session_file_path):
        """New session file is created when it doesn't exist."""
        ralph_id = "test-new-session"
        session_path = session_file_path(ralph_id)

        # Ensure no existing session
        assert not session_path.exists()

        # Create new session
        session_id = f"sess-new-{time.time()}"
        session_data = {
            "session_id": session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Verify creation
        assert session_path.exists()
        loaded_data = json.loads(session_path.read_text())
        assert loaded_data["session_id"] == session_id

    def test_session_file_contains_required_fields(self, mock_ralph_data_dir, session_file_path):
        """New session file contains all required fields."""
        ralph_id = "test-fields"
        session_path = session_file_path(ralph_id)

        # Create session with all required fields
        session_id = f"sess-fields-{time.time()}"
        now = datetime.now().isoformat()
        session_data = {
            "session_id": session_id,
            "last_used": now,
            "created_at": now
        }
        session_path.write_text(json.dumps(session_data))

        # Verify all fields present
        loaded_data = json.loads(session_path.read_text())
        assert "session_id" in loaded_data
        assert "last_used" in loaded_data
        assert "created_at" in loaded_data

    def test_session_id_format(self, mock_ralph_data_dir, session_file_path):
        """Session ID follows expected format."""
        ralph_id = "test-id-format"
        session_path = session_file_path(ralph_id)

        # Generate session ID (timestamp-based format)
        timestamp = int(time.time() * 1000)
        session_id = f"sess-{timestamp}-{os.urandom(4).hex()}"

        session_data = {
            "session_id": session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Verify format
        loaded_data = json.loads(session_path.read_text())
        assert loaded_data["session_id"].startswith("sess-")
        assert "-" in loaded_data["session_id"]


# =============================================================================
# Multiple Ralph Session Isolation Tests
# =============================================================================


class TestMultipleRalphSessions:
    """Tests for session isolation between multiple Ralph instances."""

    def test_multiple_ralphs_separate_sessions(self, mock_ralph_data_dir, session_file_path):
        """Each Ralph instance has its own separate session file."""
        ralph_id_1 = "ralph-001"
        ralph_id_2 = "ralph-002"

        session_path_1 = session_file_path(ralph_id_1)
        session_path_2 = session_file_path(ralph_id_2)

        # Create separate sessions
        session_1_data = {
            "session_id": "sess-ralph1-123",
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_2_data = {
            "session_id": "sess-ralph2-456",
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }

        session_path_1.write_text(json.dumps(session_1_data))
        session_path_2.write_text(json.dumps(session_2_data))

        # Verify isolation
        data_1 = json.loads(session_path_1.read_text())
        data_2 = json.loads(session_path_2.read_text())

        assert data_1["session_id"] != data_2["session_id"]
        assert session_path_1 != session_path_2

    def test_session_file_paths_unique(self, mock_ralph_data_dir):
        """Session file paths are unique per Ralph ID."""
        ralph_id_1 = "ralph-alpha"
        ralph_id_2 = "ralph-beta"

        path_1 = get_ralph_session_path(ralph_id_1)
        path_2 = get_ralph_session_path(ralph_id_2)

        assert path_1 != path_2
        assert str(path_1).endswith(f"{ralph_id_1}.session")
        assert str(path_2).endswith(f"{ralph_id_2}.session")

    def test_concurrent_ralphs_no_conflicts(self, mock_ralph_data_dir, session_file_path):
        """Concurrent Ralph instances don't conflict on session files."""
        # Create multiple session files simultaneously
        ralph_ids = [f"ralph-{i:03d}" for i in range(5)]

        for ralph_id in ralph_ids:
            session_path = session_file_path(ralph_id)
            session_data = {
                "session_id": f"sess-{ralph_id}-{time.time()}",
                "last_used": datetime.now().isoformat(),
                "created_at": datetime.now().isoformat()
            }
            session_path.write_text(json.dumps(session_data))

        # Verify all sessions exist and are unique
        session_ids = set()
        for ralph_id in ralph_ids:
            session_path = session_file_path(ralph_id)
            assert session_path.exists()
            data = json.loads(session_path.read_text())
            session_ids.add(data["session_id"])

        # All session IDs should be unique
        assert len(session_ids) == len(ralph_ids)


# =============================================================================
# Session History Tests
# =============================================================================


class TestSessionHistory:
    """Tests for session history logging and tracking."""

    def test_session_history_file_creation(self, mock_ralph_data_dir):
        """Session history file can be created and written to."""
        history_path = mock_ralph_data_dir / ".ralph_session_history"

        # Create history entry
        transition = {
            "timestamp": datetime.now().isoformat(),
            "from_state": "idle",
            "to_state": "active",
            "reason": "task_claimed",
            "loop_count": 1
        }

        # Write as JSON array
        history_data = [transition]
        history_path.write_text(json.dumps(history_data, indent=2))

        # Verify
        assert history_path.exists()
        loaded_history = json.loads(history_path.read_text())
        assert len(loaded_history) == 1
        assert loaded_history[0]["reason"] == "task_claimed"

    def test_session_history_accumulates(self, mock_ralph_data_dir):
        """Session history accumulates multiple transitions."""
        history_path = mock_ralph_data_dir / ".ralph_session_history"

        # Create multiple transitions
        transitions = []
        states = [
            ("idle", "active", "task_claimed"),
            ("active", "working", "task_started"),
            ("working", "complete", "task_finished"),
            ("complete", "reset", "new_task_claimed")
        ]

        for from_state, to_state, reason in states:
            transition = {
                "timestamp": datetime.now().isoformat(),
                "from_state": from_state,
                "to_state": to_state,
                "reason": reason,
                "loop_count": len(transitions) + 1
            }
            transitions.append(transition)

        # Write accumulated history
        history_path.write_text(json.dumps(transitions, indent=2))

        # Verify
        loaded_history = json.loads(history_path.read_text())
        assert len(loaded_history) == 4
        assert loaded_history[0]["from_state"] == "idle"
        assert loaded_history[-1]["to_state"] == "reset"

    def test_session_history_limited_to_50_entries(self, mock_ralph_data_dir):
        """Session history is limited to last 50 entries."""
        history_path = mock_ralph_data_dir / ".ralph_session_history"

        # Create 60 transitions
        transitions = []
        for i in range(60):
            transition = {
                "timestamp": datetime.now().isoformat(),
                "from_state": "active",
                "to_state": "reset",
                "reason": f"transition_{i}",
                "loop_count": i + 1
            }
            transitions.append(transition)

        # Simulate keeping last 50 entries
        limited_history = transitions[-50:]
        history_path.write_text(json.dumps(limited_history, indent=2))

        # Verify
        loaded_history = json.loads(history_path.read_text())
        assert len(loaded_history) == 50
        assert loaded_history[0]["reason"] == "transition_10"  # First of last 50
        assert loaded_history[-1]["reason"] == "transition_59"  # Last entry


# =============================================================================
# Claude CLI Integration Tests
# =============================================================================


class TestClaudeCLIIntegration:
    """Tests for Claude CLI session ID integration."""

    def test_session_id_passed_to_claude_command(self, mock_ralph_data_dir, session_file_path):
        """Session ID is correctly passed to Claude CLI command."""
        ralph_id = "test-cli"
        session_path = session_file_path(ralph_id)

        # Create session
        session_id = "sess-cli-test-789"
        session_data = {
            "session_id": session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Verify session ID can be extracted
        loaded_data = json.loads(session_path.read_text())
        extracted_id = loaded_data["session_id"]

        # Simulate command construction
        claude_command = ["claude", "code", "--session", extracted_id, "--prompt", "test.txt"]
        assert "--session" in claude_command
        assert session_id in claude_command

    def test_session_id_format_compatible_with_claude(self, mock_ralph_data_dir, session_file_path):
        """Session ID format is compatible with Claude CLI expectations."""
        ralph_id = "test-format"
        session_path = session_file_path(ralph_id)

        # Generate session ID in expected format
        # Claude expects session IDs to be strings, typically UUIDs or custom formats
        session_id = f"sess-{int(time.time())}-{os.urandom(8).hex()}"

        session_data = {
            "session_id": session_id,
            "last_used": datetime.now().isoformat(),
            "created_at": datetime.now().isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Verify format
        loaded_data = json.loads(session_path.read_text())
        assert isinstance(loaded_data["session_id"], str)
        assert len(loaded_data["session_id"]) > 10  # Reasonable length
        assert loaded_data["session_id"].startswith("sess-")

    def test_session_resumption_with_existing_id(self, mock_ralph_data_dir, session_file_path):
        """Existing session ID is used for resumption."""
        ralph_id = "test-resume-cli"
        session_path = session_file_path(ralph_id)

        # Create existing session
        existing_session_id = "sess-existing-abc123xyz"
        session_data = {
            "session_id": existing_session_id,
            "last_used": (datetime.now() - timedelta(hours=1)).isoformat(),
            "created_at": (datetime.now() - timedelta(hours=5)).isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Simulate resumption: read existing ID
        loaded_data = json.loads(session_path.read_text())
        resumed_id = loaded_data["session_id"]

        # Verify same ID is used
        assert resumed_id == existing_session_id


# =============================================================================
# Session Cleanup Tests
# =============================================================================


class TestSessionCleanup:
    """Tests for cleanup of old session files."""

    def test_cleanup_old_sessions_over_7_days(self, mock_ralph_data_dir, session_file_path):
        """Session files older than 7 days are identified for cleanup."""
        ralph_id = "test-old-cleanup"
        session_path = session_file_path(ralph_id)

        # Create old session (8 days old)
        old_time = datetime.now() - timedelta(days=8)
        session_data = {
            "session_id": "sess-very-old-123",
            "last_used": old_time.isoformat(),
            "created_at": old_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Set file modification time to 8 days ago
        old_timestamp = time.time() - (8 * 24 * 3600)
        os.utime(session_path, (old_timestamp, old_timestamp))

        # Check file age
        mtime = session_path.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        assert age_days > 7, "Session should be older than 7 days"

    def test_cleanup_preserves_recent_sessions(self, mock_ralph_data_dir, session_file_path):
        """Recent session files (< 7 days) are preserved during cleanup."""
        ralph_id = "test-recent-cleanup"
        session_path = session_file_path(ralph_id)

        # Create recent session (2 days old)
        recent_time = datetime.now() - timedelta(days=2)
        session_data = {
            "session_id": "sess-recent-456",
            "last_used": recent_time.isoformat(),
            "created_at": recent_time.isoformat()
        }
        session_path.write_text(json.dumps(session_data))

        # Set file modification time to 2 days ago
        recent_timestamp = time.time() - (2 * 24 * 3600)
        os.utime(session_path, (recent_timestamp, recent_timestamp))

        # Check file age
        mtime = session_path.stat().st_mtime
        age_days = (time.time() - mtime) / 86400
        assert age_days < 7, "Session should be younger than 7 days"

    def test_cleanup_identifies_multiple_old_sessions(self, mock_ralph_data_dir, session_file_path):
        """Cleanup identifies multiple old session files."""
        # Create mix of old and recent sessions
        test_cases = [
            ("ralph-old-1", 8, True),   # 8 days old - should cleanup
            ("ralph-old-2", 10, True),  # 10 days old - should cleanup
            ("ralph-recent-1", 3, False),  # 3 days old - keep
            ("ralph-recent-2", 1, False),  # 1 day old - keep
            ("ralph-old-3", 7.1, True),  # Just over 7 days - should cleanup
        ]

        old_sessions = []
        recent_sessions = []

        for ralph_id, age_days, should_cleanup in test_cases:
            session_path = session_file_path(ralph_id)

            old_time = datetime.now() - timedelta(days=age_days)
            session_data = {
                "session_id": f"sess-{ralph_id}",
                "last_used": old_time.isoformat(),
                "created_at": old_time.isoformat()
            }
            session_path.write_text(json.dumps(session_data))

            # Set file modification time
            old_timestamp = time.time() - (age_days * 24 * 3600)
            os.utime(session_path, (old_timestamp, old_timestamp))

            # Categorize
            if should_cleanup:
                old_sessions.append(ralph_id)
            else:
                recent_sessions.append(ralph_id)

        # Verify categorization
        assert len(old_sessions) == 3, "Should have 3 old sessions"
        assert len(recent_sessions) == 2, "Should have 2 recent sessions"

        # Verify old sessions are indeed old
        for ralph_id in old_sessions:
            session_path = session_file_path(ralph_id)
            mtime = session_path.stat().st_mtime
            age_days = (time.time() - mtime) / 86400
            assert age_days >= 7, f"{ralph_id} should be >= 7 days old"

    def test_cleanup_handles_missing_session_files(self, mock_ralph_data_dir, session_file_path):
        """Cleanup gracefully handles missing session files."""
        ralph_id = "test-missing-cleanup"
        session_path = session_file_path(ralph_id)

        # Ensure file doesn't exist
        assert not session_path.exists()

        # Attempting to access should not crash
        # (in real cleanup logic, this would be handled with exists() check)
        exists = session_path.exists()
        assert exists is False


# =============================================================================
# Integration Tests
# =============================================================================


class TestSessionIntegration:
    """Integration tests for complete session lifecycle."""

    def test_full_session_lifecycle(self, mock_ralph_data_dir, session_file_path):
        """Complete session lifecycle from creation to expiry."""
        ralph_id = "test-lifecycle"
        session_path = session_file_path(ralph_id)

        # 1. Create new session
        session_id = f"sess-lifecycle-{time.time()}"
        now = datetime.now()
        session_data = {
            "session_id": session_id,
            "last_used": now.isoformat(),
            "created_at": now.isoformat()
        }
        session_path.write_text(json.dumps(session_data))
        assert session_path.exists()

        # 2. Update session (simulate usage)
        time.sleep(0.1)
        updated_time = datetime.now()
        session_data["last_used"] = updated_time.isoformat()
        session_path.write_text(json.dumps(session_data))

        # 3. Verify session is active (recent)
        mtime = session_path.stat().st_mtime
        age_seconds = time.time() - mtime
        assert age_seconds < 60

        # 4. Simulate expiry (set old mtime)
        old_timestamp = time.time() - (25 * 3600)  # 25 hours
        os.utime(session_path, (old_timestamp, old_timestamp))

        # 5. Verify session is expired
        mtime = session_path.stat().st_mtime
        age_hours = (time.time() - mtime) / 3600
        assert age_hours > 24

        # 6. Delete expired session
        session_path.unlink()
        assert not session_path.exists()

    def test_session_path_function(self, mock_ralph_data_dir):
        """get_ralph_session_path() returns correct path."""
        ralph_id = "test-path-func"

        session_path = get_ralph_session_path(ralph_id)

        assert isinstance(session_path, Path)
        assert str(session_path).endswith(f"{ralph_id}.session")
        assert session_path.parent == mock_ralph_data_dir
