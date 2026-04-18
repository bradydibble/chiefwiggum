"""Tests for daemon intent tables + helpers.

Covers the durable TUI → daemon channel added in Phase 2:
  - spawn_requests / cancel_requests tables exist
  - enqueue / fetch / mark_consumed round-trip
  - pending queue is filtered by consumed_at
  - priority + age ordering
  - count_pending_intents aggregates correctly
"""

import asyncio
import os

import pytest

from chiefwiggum import init_db, reset_db
from chiefwiggum.coordination import (
    count_pending_intents,
    enqueue_cancel_request,
    enqueue_spawn_request,
    fetch_pending_cancel_requests,
    fetch_pending_spawn_requests,
    mark_cancel_request_consumed,
    mark_spawn_request_consumed,
)


@pytest.fixture(autouse=True)
async def setup_test_db(tmp_path):
    test_db = tmp_path / "test_intents.db"
    os.environ["CHIEFWIGGUM_DB"] = str(test_db)
    await init_db()
    yield
    await reset_db()
    del os.environ["CHIEFWIGGUM_DB"]


class TestSpawnRequests:
    @pytest.mark.asyncio
    async def test_enqueue_returns_positive_id(self):
        req_id = await enqueue_spawn_request(
            project_path="/tmp/demo",
            fix_plan_path="/tmp/demo/fix_plan.md",
            priority=0,
            requested_by="cli",
        )
        assert req_id > 0

    @pytest.mark.asyncio
    async def test_pending_list_contains_new_request(self):
        await enqueue_spawn_request(project_path="/tmp/demo", requested_by="cli")
        pending = await fetch_pending_spawn_requests()
        assert len(pending) == 1
        assert pending[0]["project_path"] == "/tmp/demo"
        assert pending[0]["requested_by"] == "cli"
        assert pending[0]["task_id"] is None

    @pytest.mark.asyncio
    async def test_consumed_request_disappears_from_pending(self):
        req_id = await enqueue_spawn_request(project_path="/tmp/demo")
        await mark_spawn_request_consumed(req_id, spawned_ralph_id="host-abc")
        pending = await fetch_pending_spawn_requests()
        assert pending == []

    @pytest.mark.asyncio
    async def test_consumed_records_error_on_failure(self):
        req_id = await enqueue_spawn_request(project_path="/tmp/demo")
        await mark_spawn_request_consumed(req_id, error="spawn failed: no fix_plan")
        pending = await fetch_pending_spawn_requests()
        assert pending == []

    @pytest.mark.asyncio
    async def test_priority_sorting_higher_first(self):
        low_id = await enqueue_spawn_request(project_path="/tmp/low", priority=0)
        high_id = await enqueue_spawn_request(project_path="/tmp/high", priority=10)
        await asyncio.sleep(0.01)  # ensure distinct requested_at
        mid_id = await enqueue_spawn_request(project_path="/tmp/mid", priority=5)

        pending = await fetch_pending_spawn_requests()
        priorities = [r["priority"] for r in pending]
        assert priorities == sorted(priorities, reverse=True)
        assert pending[0]["id"] == high_id
        assert pending[-1]["id"] == low_id
        assert mid_id in {r["id"] for r in pending}

    @pytest.mark.asyncio
    async def test_age_breaks_priority_tie(self):
        first = await enqueue_spawn_request(project_path="/tmp/first", priority=1)
        await asyncio.sleep(0.02)
        second = await enqueue_spawn_request(project_path="/tmp/second", priority=1)

        pending = await fetch_pending_spawn_requests()
        ids = [r["id"] for r in pending]
        assert ids == [first, second], "older request should come first at equal priority"

    @pytest.mark.asyncio
    async def test_specific_task_id_preserved(self):
        await enqueue_spawn_request(
            project_path="/tmp/demo",
            task_id="task-42",
            requested_by="tui",
        )
        pending = await fetch_pending_spawn_requests()
        assert pending[0]["task_id"] == "task-42"
        assert pending[0]["requested_by"] == "tui"


class TestCancelRequests:
    @pytest.mark.asyncio
    async def test_enqueue_and_consume_round_trip(self):
        req_id = await enqueue_cancel_request(ralph_id="host-xyz", requested_by="tui")
        assert req_id > 0

        pending = await fetch_pending_cancel_requests()
        assert len(pending) == 1
        assert pending[0]["ralph_id"] == "host-xyz"

        await mark_cancel_request_consumed(req_id)
        assert await fetch_pending_cancel_requests() == []


class TestConfigPassthrough:
    """The TUI encodes RalphConfig/TargetingConfig as JSON into the intent row
    so the daemon can reconstruct them and honor the user's wizard choices."""

    @pytest.mark.asyncio
    async def test_config_and_targeting_json_round_trip(self):
        from chiefwiggum.coordination import fetch_pending_spawn_requests
        from chiefwiggum.models import (
            ClaudeModel,
            RalphConfig,
            TargetingConfig,
            TaskCategory,
            TaskPriority,
        )

        ralph_config = RalphConfig(
            model=ClaudeModel.OPUS,
            no_continue=True,
            session_expiry_hours=48,
            max_calls_per_hour=200,
        )
        targeting = TargetingConfig(
            project="tian",
            priority_min=TaskPriority.HIGH,
            categories=[TaskCategory.TESTING],
        )

        await enqueue_spawn_request(
            project_path="tian",
            requested_by="tui",
            config_json=ralph_config.model_dump_json(),
            targeting_json=targeting.model_dump_json(),
        )
        pending = await fetch_pending_spawn_requests()
        assert len(pending) == 1
        assert pending[0]["config_json"] is not None
        assert pending[0]["targeting_json"] is not None

        restored_config = RalphConfig.model_validate_json(pending[0]["config_json"])
        restored_targeting = TargetingConfig.model_validate_json(pending[0]["targeting_json"])
        assert restored_config.model == ClaudeModel.OPUS
        assert restored_config.session_expiry_hours == 48
        assert restored_config.max_calls_per_hour == 200
        assert restored_targeting.project == "tian"
        assert restored_targeting.priority_min == TaskPriority.HIGH
        assert restored_targeting.categories == [TaskCategory.TESTING]

    @pytest.mark.asyncio
    async def test_missing_config_json_is_none(self):
        from chiefwiggum.coordination import fetch_pending_spawn_requests

        await enqueue_spawn_request(project_path="demo")
        pending = await fetch_pending_spawn_requests()
        assert pending[0]["config_json"] is None
        assert pending[0]["targeting_json"] is None


class TestCountPendingIntents:
    @pytest.mark.asyncio
    async def test_empty_queues_return_zeros(self):
        counts = await count_pending_intents()
        assert counts == {"spawn": 0, "cancel": 0}

    @pytest.mark.asyncio
    async def test_count_reflects_pending_only(self):
        consumed_id = await enqueue_spawn_request(project_path="/tmp/a")
        await mark_spawn_request_consumed(consumed_id, spawned_ralph_id="r1")
        await enqueue_spawn_request(project_path="/tmp/b")
        await enqueue_spawn_request(project_path="/tmp/c")
        await enqueue_cancel_request(ralph_id="r2")

        counts = await count_pending_intents()
        assert counts == {"spawn": 2, "cancel": 1}
