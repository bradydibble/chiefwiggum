"""Tests for the single Python RALPH_STATUS parser."""

from __future__ import annotations

from pathlib import Path

import pytest

from chiefwiggum.completion import (
    RalphStatus,
    find_latest_ralph_status,
    parse_ralph_status,
    parse_ralph_status_from_file,
)


class TestParseRalphStatus:
    def test_full_complete_block(self):
        text = """
Some chatter.
---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: task-52-verification-steps
COMMIT: 7c721147bf52e4a0be6612bc6d5baa9ccb6ad2e7
VERIFICATION: Tier 4 targeted tests pass
---END_RALPH_STATUS---
trailing junk
"""
        s = parse_ralph_status(text)
        assert s is not None
        assert s.status == "COMPLETE"
        assert s.exit_signal is True
        assert s.task_id == "task-52-verification-steps"
        assert s.commit_sha == "7c721147bf52e4a0be6612bc6d5baa9ccb6ad2e7"
        assert "Tier 4" in s.verification
        assert s.is_complete is True
        assert s.is_failure is False

    def test_short_commit_sha_preserved(self):
        text = """---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: task-53-files-to-create
COMMIT: 7d1351a
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.commit_sha == "7d1351a"

    def test_no_block_returns_none(self):
        assert parse_ralph_status("nothing here") is None

    def test_block_without_task_id_still_parses(self):
        text = """---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.task_id is None
        assert s.is_complete is True

    def test_exit_signal_false_is_parsed(self):
        text = """---RALPH_STATUS---
STATUS: CONTINUE
EXIT_SIGNAL: false
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.exit_signal is False
        assert s.is_complete is False

    def test_fail_status_marks_failure(self):
        text = """---RALPH_STATUS---
STATUS: FAIL
TASK_ID: task-99
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.is_failure is True
        assert s.is_complete is False

    def test_null_task_id_becomes_none(self):
        text = """---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: null
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.task_id is None

    def test_malformed_lines_are_ignored(self):
        text = """---RALPH_STATUS---
STATUS: COMPLETE
EXIT_SIGNAL: true
TASK_ID: task-7
garbage line without colon
COMMIT: abc123
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.status == "COMPLETE"
        assert s.task_id == "task-7"
        assert s.commit_sha == "abc123"

    def test_only_first_block_is_used(self):
        """Claude's streaming JSON sometimes embeds the block twice. We
        take the first — subsequent duplicates should not override."""
        text = """---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: first-task
---END_RALPH_STATUS---
padding
---RALPH_STATUS---
STATUS: COMPLETE
TASK_ID: second-task
---END_RALPH_STATUS---"""
        s = parse_ralph_status(text)
        assert s.task_id == "first-task"


class TestFileHelpers:
    def test_parse_from_file(self, tmp_path):
        p = tmp_path / "out.log"
        p.write_text(
            "---RALPH_STATUS---\nSTATUS: COMPLETE\nTASK_ID: t1\n---END_RALPH_STATUS---\n"
        )
        s = parse_ralph_status_from_file(p)
        assert s is not None
        assert s.task_id == "t1"

    def test_parse_from_missing_file_returns_none(self, tmp_path):
        assert parse_ralph_status_from_file(tmp_path / "nope.log") is None

    def test_find_latest_picks_newest(self, tmp_path):
        import os

        (tmp_path / "claude_output_old.log").write_text(
            "---RALPH_STATUS---\nSTATUS: COMPLETE\nTASK_ID: old-task\n---END_RALPH_STATUS---"
        )
        # Force a distinct earlier mtime.
        old = tmp_path / "claude_output_old.log"
        os.utime(old, (old.stat().st_atime - 10, old.stat().st_mtime - 10))

        new = tmp_path / "claude_output_new.log"
        new.write_text(
            "---RALPH_STATUS---\nSTATUS: COMPLETE\nTASK_ID: new-task\n---END_RALPH_STATUS---"
        )

        result = find_latest_ralph_status(tmp_path)
        assert result is not None
        path, parsed = result
        assert path == new
        assert parsed.task_id == "new-task"

    def test_find_latest_skips_files_without_block(self, tmp_path):
        (tmp_path / "claude_output_one.log").write_text("chatter, no block")
        (tmp_path / "claude_output_two.log").write_text(
            "---RALPH_STATUS---\nSTATUS: COMPLETE\nTASK_ID: has-block\n---END_RALPH_STATUS---"
        )
        result = find_latest_ralph_status(tmp_path)
        assert result is not None
        assert result[1].task_id == "has-block"

    def test_find_latest_no_matches_returns_none(self, tmp_path):
        (tmp_path / "claude_output_one.log").write_text("no block here")
        assert find_latest_ralph_status(tmp_path) is None
