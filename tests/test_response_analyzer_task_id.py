"""Regression: the shell analyzer must extract TASK_ID from a RALPH_STATUS
block, not silently fall back to scraping `task-N` out of the commit
message. The short form is not the stable id in the DB, so `wig complete
task-52` fails and (as of the 2026-04-18 outage) kills the worker.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ANALYZER = REPO_ROOT / "chiefwiggum" / "scripts" / "lib" / "response_analyzer.sh"
DATE_UTILS = REPO_ROOT / "chiefwiggum" / "scripts" / "lib" / "date_utils.sh"


def _run_analyzer(output_file: Path, loop: int = 1) -> dict:
    """Source the analyzer, run analyze_response on a fake claude output
    file, and return the parsed .response_analysis JSON."""
    result_path = output_file.parent / ".response_analysis"
    script = f"""
set -e
source "{DATE_UTILS}"
source "{ANALYZER}"
cd "{output_file.parent}"
mkdir -p logs
cp "{output_file}" "logs/{output_file.name}"
analyze_response "logs/{output_file.name}" "{loop}" ".response_analysis" >/dev/null 2>&1 || true
cat .response_analysis
"""
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        cwd=output_file.parent,
    )
    assert proc.returncode == 0, f"analyzer failed: {proc.stderr}"
    return json.loads(proc.stdout)


class TestRalphStatusTaskIdExtraction:
    def test_extracts_full_stable_task_id(self, tmp_path):
        """The RALPH_STATUS block names task-52-verification-steps. The
        analyzer should propagate that exact id, not fall back to
        scraping 'task-52' from an arbitrary commit message."""
        output = tmp_path / "claude_output_test.log"
        output.write_text(
            "Some chatter Claude wrote before the block.\n"
            "---RALPH_STATUS---\n"
            "STATUS: COMPLETE\n"
            "EXIT_SIGNAL: true\n"
            "TASK_ID: task-52-verification-steps\n"
            "COMMIT: 7c721147bf52e4a0be6612bc6d5baa9ccb6ad2e7\n"
            "VERIFICATION: Tier 4 targeted tests pass\n"
            "---END_RALPH_STATUS---\n"
        )

        result = _run_analyzer(output)

        # The happy path: exact stable id flows through.
        assert result["analysis"]["completed_task_id"] == "task-52-verification-steps"
        assert result["analysis"]["completed_commit_sha"] == (
            "7c721147bf52e4a0be6612bc6d5baa9ccb6ad2e7"
        )
        assert result["analysis"]["exit_signal"] is True
        assert result["analysis"]["has_completion_signal"] is True

    def test_extracts_commit_sha_even_when_short(self, tmp_path):
        """Claude sometimes emits a short SHA. Propagate whatever was
        written verbatim."""
        output = tmp_path / "claude_output_short.log"
        output.write_text(
            "---RALPH_STATUS---\n"
            "STATUS: COMPLETE\n"
            "EXIT_SIGNAL: true\n"
            "TASK_ID: task-53-files-to-create\n"
            "COMMIT: 7d1351a\n"
            "---END_RALPH_STATUS---\n"
        )
        result = _run_analyzer(output)
        assert result["analysis"]["completed_task_id"] == "task-53-files-to-create"
        assert result["analysis"]["completed_commit_sha"] == "7d1351a"

    def test_block_without_task_id_does_not_break_parser(self, tmp_path):
        """Claude occasionally omits TASK_ID. Should NOT crash and SHOULD
        still set exit_signal based on STATUS/EXIT_SIGNAL."""
        output = tmp_path / "claude_output_no_taskid.log"
        output.write_text(
            "---RALPH_STATUS---\n"
            "STATUS: COMPLETE\n"
            "EXIT_SIGNAL: true\n"
            "---END_RALPH_STATUS---\n"
        )
        result = _run_analyzer(output)
        assert result["analysis"]["exit_signal"] is True
        assert result["analysis"]["completed_task_id"] == ""
