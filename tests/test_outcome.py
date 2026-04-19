"""Tests for the worker → daemon outcome file protocol."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chiefwiggum.outcome import (
    WorkerExitStatus,
    WorkerOutcome,
    consume_outcome,
    get_outcome_path,
    list_pending_outcomes,
    read_outcome,
    write_outcome,
)


@pytest.fixture(autouse=True)
def isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    from chiefwiggum.paths import reset_paths_cache
    reset_paths_cache()
    yield tmp_path
    reset_paths_cache()


class TestSerialization:
    def test_round_trip_success(self):
        oc = WorkerOutcome(
            ralph_id="r1",
            status=WorkerExitStatus.SUCCESS,
            task_id="task-52-verification-steps",
            commit_sha="abc1234",
            started_at="2026-04-19T12:00:00Z",
            ended_at="2026-04-19T12:15:00Z",
            loops_run=3,
            total_cost_usd=0.42,
        )
        restored = WorkerOutcome.from_json(oc.to_json())
        assert restored == oc
        assert restored.status is WorkerExitStatus.SUCCESS

    def test_round_trip_failure(self):
        oc = WorkerOutcome(
            ralph_id="r2",
            status=WorkerExitStatus.FAILED,
            task_id="task-7",
            error_category="code_error",
            error_message="pytest failed 3 times",
        )
        restored = WorkerOutcome.from_json(oc.to_json())
        assert restored == oc

    def test_unknown_fields_ignored(self):
        """A newer daemon reading an older worker's file (or vice versa)
        must not blow up on unknown keys."""
        raw = json.dumps({
            "ralph_id": "r3",
            "status": "success",
            "task_id": "t1",
            "mystery_future_field": 42,
        })
        restored = WorkerOutcome.from_json(raw)
        assert restored.ralph_id == "r3"
        assert restored.status is WorkerExitStatus.SUCCESS


class TestAtomicWrite:
    def test_write_then_read_round_trip(self, isolate_paths):
        oc = WorkerOutcome(
            ralph_id="atomic-test",
            status=WorkerExitStatus.SUCCESS,
            task_id="task-1",
        )
        path = write_outcome(oc)
        restored = read_outcome(path)
        assert restored == oc

    def test_write_leaves_no_tempfile_on_success(self, isolate_paths):
        oc = WorkerOutcome(
            ralph_id="cleanup-test",
            status=WorkerExitStatus.SUCCESS,
        )
        path = write_outcome(oc)
        leftovers = list(path.parent.glob(f".{oc.ralph_id}.outcome.*.json.tmp"))
        assert leftovers == []

    def test_read_missing_file_returns_none(self, isolate_paths):
        assert read_outcome(Path(isolate_paths) / "nope.json") is None

    def test_read_malformed_file_returns_none(self, isolate_paths):
        p = isolate_paths / "garbage.outcome.json"
        p.write_text("not json at all {{{")
        assert read_outcome(p) is None

    def test_list_pending_finds_outcomes(self, isolate_paths):
        w1 = WorkerOutcome(ralph_id="r1", status=WorkerExitStatus.SUCCESS)
        w2 = WorkerOutcome(ralph_id="r2", status=WorkerExitStatus.FAILED)
        write_outcome(w1)
        write_outcome(w2)
        pending = list_pending_outcomes()
        assert len(pending) == 2
        names = {p.name for p in pending}
        assert "r1.outcome.json" in names
        assert "r2.outcome.json" in names

    def test_consume_removes_file(self, isolate_paths):
        oc = WorkerOutcome(ralph_id="consume-test", status=WorkerExitStatus.SUCCESS)
        path = write_outcome(oc)
        assert path.exists()
        consume_outcome(path)
        assert not path.exists()

    def test_outcome_path_under_ralphs_dir(self, isolate_paths):
        # Explicitly uses the paths.ralphs_dir tree the daemon already scans.
        p = get_outcome_path("ralph-xyz")
        assert p.name == "ralph-xyz.outcome.json"
        assert p.parent.name == "ralphs"
